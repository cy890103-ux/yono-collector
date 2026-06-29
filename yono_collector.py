#!/usr/bin/env python3
"""
YONO Collector — Pexels → 飞书 Asset 表格
按 OS.md (YONO Playbook v1.0) 标准自动分类、评分、标注

配置全部从 config/ 目录的 YAML 文件读取：
  config/keywords.yaml  → 搜索关键词、分类映射、模糊匹配规则
  config/judge.yaml     → 六大模块的评分、原因、标签标准
  config/sources.yaml   → Pexels API Key、飞书连接信息

改配置只改 YAML，不用动这个脚本。

Usage:
  python3 yono_collector.py                  # 默认搜5张
  python3 yono_collector.py --keyword "sneaker design" --count 10
  python3 yono_collector.py --list           # 查看飞书表格现有记录数
  python3 yono_collector.py --backfill       # 回填已有记录的空字段
  python3 yono_collector.py --dry-run        # 只预览评分，不写入飞书
"""

import requests
import json
import time
import os
import sys
import argparse
import tempfile
import yaml

# ─── 配置文件路径 ───
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(SCRIPT_DIR, "config")


def load_configs():
    """从 YAML 文件加载所有配置"""
    keywords_cfg = yaml.safe_load(open(os.path.join(CONFIG_DIR, "keywords.yaml")))
    judge_cfg = yaml.safe_load(open(os.path.join(CONFIG_DIR, "judge.yaml")))
    sources_cfg = yaml.safe_load(open(os.path.join(CONFIG_DIR, "sources.yaml")))
    return keywords_cfg, judge_cfg, sources_cfg


def get_feishu_credentials(sources_cfg):
    """从配置获取飞书连接信息"""
    return {
        "app_id": sources_cfg["feishu"]["app_id"],
        "app_secret": sources_cfg["feishu"]["app_secret"],
        "app_token": sources_cfg["feishu"]["app_token"],
        "table_id": sources_cfg["feishu"]["table_id"],
    }


def get_pexels_config(sources_cfg):
    """从配置获取 Pexels 搜索参数"""
    return {
        "api_key": sources_cfg["pexels"]["api_key"],
        "base_url": sources_cfg["pexels"]["base_url"],
        "orientation": sources_cfg["pexels"]["orientation"],
        "image_size": sources_cfg["pexels"]["image_size"],
    }


def classify_category(keyword, keywords_cfg):
    """根据搜索关键词自动判断 Category 分类"""
    # 1) 精确匹配：自定义映射
    custom_map = keywords_cfg.get("custom_mapping", {})
    if keyword in custom_map:
        return custom_map[keyword]

    # 2) 每日轮换关键词精确匹配
    rotation = keywords_cfg.get("daily_rotation", {})
    for day, info in rotation.items():
        if info["keyword"] == keyword:
            return info["category"]

    # 3) 语义关键词模糊匹配
    kw_lower = keyword.lower()
    fuzzy = keywords_cfg.get("fuzzy_match", {})
    fallback = keywords_cfg.get("fallback_category", "Tape")
    best_category = fallback
    best_score = 0
    for category, tags in fuzzy.items():
        score = sum(1 for tag in tags if tag in kw_lower)
        if score > best_score:
            best_score = score
            best_category = category

    return best_category


def get_judge_fields(keyword, keywords_cfg, judge_cfg, category=None, photo=None):
    """根据关键词 + 分类 + 图片元数据，生成 OS.md 标准的评判字段"""
    if category is None:
        category = classify_category(keyword, keywords_cfg)

    modules = judge_cfg.get("modules", {})
    judge = modules.get(category, modules.get("Tape", {}))
    default_status = judge_cfg.get("default_status", "Raw｜未判断")
    quality = evaluate_photo_quality(keyword, category, photo, judge, judge_cfg)

    return {
        "Category｜分类": category,
        "Status｜状态": quality["status"] or default_status,
        "Score｜评分": quality["score"],
        "YONO Reason｜为什么适合 YONO": quality["reason"],
        "标题": quality["title"],
        "Content Angle｜内容角度": quality["content_angle"],
        "Visual Tags｜视觉标签": judge["visual_tags"],
        "Mood Tags｜情绪标签": judge["mood_tags"],
    }


def evaluate_photo_quality(keyword, category, photo, judge, judge_cfg):
    """按每张图片独立评分。

    设计原则：
    - 模块 score 只是基础分，不是最终分。
    - 4/5 分必须有清晰 YONO 信号或分类信号支撑。
    - 尺寸太低、alt 太弱、商业素材感太强的图会被压分。
    """
    model = judge_cfg.get("quality_model", {})
    base_score = int(judge.get("score", 3))
    if not model.get("enabled", True) or not photo:
        return {
            "score": base_score,
            "status": judge_cfg.get("default_status", "Raw｜未判断"),
            "reason": judge.get("yono_reason", ""),
            "title": "",
            "content_angle": "",
        }

    alt = str(photo.get("alt") or "").strip()
    width = int(photo.get("width") or 0)
    height = int(photo.get("height") or 0)
    short_edge = min(width, height) if width and height else 0
    # 评分只看图片自身描述，避免搜索关键词把普通图片误抬到高分。
    # keyword 仍用于分类，但不作为图片质量证据。
    text = normalize_text(alt)

    positive_hits = matched_terms(text, model.get("yono_positive_signals", []))
    anti_hits = matched_terms(text, model.get("anti_signals", []))
    category_hits = matched_terms(
        text,
        model.get("category_signals", {}).get(category, [])
    )

    score = base_score
    evidence = []

    high_res = int(model.get("high_resolution_min_short_edge", 1400))
    low_res = int(model.get("low_resolution_min_short_edge", 900))

    if short_edge >= high_res:
        score += 1
        evidence.append(f"清晰度达标，短边 {short_edge}px")
    elif short_edge and short_edge < low_res:
        score -= 1
        evidence.append(f"分辨率偏低，短边 {short_edge}px")

    if len(alt) < 12:
        score -= 1
        evidence.append("图片描述过弱，无法支撑高分")
    else:
        evidence.append(f"图片描述：{alt[:80]}")

    if len(positive_hits) >= 3:
        score += 1
        evidence.append(f"YONO 信号明确：{', '.join(positive_hits[:4])}")
    elif len(positive_hits) == 0:
        score -= 1
        evidence.append("缺少温度、材质、安静日常等 YONO 信号")

    if len(category_hits) >= 2:
        score += 1
        evidence.append(f"分类相关性强：{', '.join(category_hits[:4])}")
    elif len(category_hits) == 0:
        score -= 1
        evidence.append(f"和 {category} 分类的直接关系较弱")

    if anti_hits:
        score -= min(2, len(anti_hits))
        evidence.append(f"反向信号：{', '.join(anti_hits[:3])}")

    # 没有足够证据时不允许高分。避免“关键词好但图片普通”直接进 Save。
    if len(positive_hits) < 2 and len(category_hits) < 2:
        score = min(score, 3)
    if anti_hits:
        score = min(score, 3)
    if len(alt) < 12:
        score = min(score, 2)

    score = max(1, min(5, score))
    status = status_for_score(score, model, judge_cfg)
    curiosity_point = build_curiosity_point(category, alt, positive_hits, category_hits)
    reason = build_selection_reason(
        judge.get("yono_reason", ""),
        evidence,
        score,
        curiosity_point
    )
    title = ""
    content_angle = ""
    if status == "Save｜保存":
        title = build_xiaohongshu_title(category, alt, positive_hits, category_hits)
        content_angle = build_xiaohongshu_content_angle(
            category,
            alt,
            reason,
            curiosity_point
        )

    return {
        "score": score,
        "status": status,
        "reason": reason,
        "title": title,
        "content_angle": content_angle,
    }


def normalize_text(value):
    return " ".join(str(value).lower().replace("_", " ").replace("-", " ").split())


def matched_terms(text, terms):
    hits = []
    for term in terms:
        term_text = normalize_text(term)
        if term_text and term_text in text and term not in hits:
            hits.append(term)
    return hits


def status_for_score(score, model, judge_cfg):
    mapping = model.get("status_by_score", {})
    return (
        mapping.get(score)
        or mapping.get(str(score))
        or judge_cfg.get("default_status", "Raw｜未判断")
    )


def build_selection_reason(base_reason, evidence, score, curiosity_point):
    verdict = {
        5: "筛选理由：这张图高度值得保留。",
        4: "筛选理由：这张图适合进入素材库。",
        3: "筛选理由：这张图有可用价值，但需要人工复核。",
        2: "筛选理由：这张图 YONO 信号偏弱，暂不建议保留。",
        1: "筛选理由：这张图不符合当前筛选标准。",
    }.get(score, "已按 YONO 标准评估。")
    evidence_text = "；".join(evidence[:4])
    curiosity_text = f"好奇心点：{curiosity_point}" if curiosity_point else "好奇心点：暂不明确"
    return f"{verdict} {base_reason} 判断依据：{evidence_text}。{curiosity_text}"


def build_curiosity_point(category, alt, positive_hits, category_hits):
    """生成“它的好奇心点在哪里”。"""
    if category_hits:
        anchor = "、".join(category_hits[:2])
    elif positive_hits:
        anchor = "、".join(positive_hits[:2])
    else:
        anchor = ""

    if category == "Postcard":
        lens = "画面里的情绪为什么值得被保存"
    elif category == "Archive":
        lens = "它背后的时间感、出处或设计脉络是什么"
    elif category == "Field Notes":
        lens = "这个日常观察能引出什么生活或品牌思考"
    elif category == "OOTD":
        lens = "人与物、穿搭和生活方式之间有什么关系"
    elif category == "Block":
        lens = "材质、结构或物件细节为什么让人想多看一眼"
    else:
        lens = "这个氛围为什么会让人停下来"

    if anchor:
        return f"从 {anchor} 切入，追问：{lens}。"

    if alt:
        return f"从画面描述“{alt[:48]}”切入，追问：{lens}。"

    return f"追问：{lens}。"


def build_xiaohongshu_title(category, alt, positive_hits, category_hits):
    """只给 Save 素材生成小红书方向标题。"""
    if category_hits:
        anchor = display_signal(category_hits[0])
    elif positive_hits:
        anchor = display_signal(positive_hits[0])
    else:
        anchor = category_label(category)

    templates = {
        "Postcard": f"这张{anchor}感照片，像把情绪轻轻收起来",
        "Archive": f"被这张{anchor}打中：有些设计真的越看越想收藏",
        "Field Notes": f"一个{anchor}细节，突然让我想重新观察日常",
        "OOTD": f"{anchor}不只是穿搭，是一种生活方式信号",
        "Block": f"这个{anchor}细节，才是照片真正耐看的地方",
        "Tape": f"这张图有种{anchor}的安静后劲",
    }
    title = templates.get(category, f"这张图的{anchor}感，值得存进素材库")

    # 如果图片描述里有更具体的物件，补一点画面感，但控制长度。
    object_hint = extract_object_hint(alt)
    if object_hint and object_hint.lower() not in title.lower():
        title = f"{title}｜{object_hint}"

    return title[:60]


def build_xiaohongshu_content_angle(category, alt, reason, curiosity_point):
    """只给 Save 素材生成小红书选题角度。"""
    category_prompt = {
        "Postcard": "从情绪收藏切入：这张图为什么让人想停一下、存下来、发给某个人。",
        "Archive": "从收藏档案切入：拆它的时间感、设计感、材质感，以及为什么值得长期保存。",
        "Field Notes": "从日常观察切入：把一个普通场景讲成一个生活方式或品牌洞察。",
        "OOTD": "从生活方式切入：讲人与物、穿搭、姿态和审美选择之间的关系。",
        "Block": "从物件细节切入：放大材质、结构、纹理和手作感，讲为什么它耐看。",
        "Tape": "从氛围记忆切入：讲这张图带来的声音感、片段感和情绪后劲。",
    }.get(category, "从画面里最耐看的细节切入，讲它为什么值得被保存。")

    alt_text = f"画面依据：{alt[:90]}。" if alt else ""
    return f"{category_prompt} {alt_text}{curiosity_point}".strip()


def category_label(category):
    labels = {
        "Postcard": "情绪",
        "Archive": "收藏",
        "Field Notes": "观察",
        "OOTD": "穿搭",
        "Block": "材质",
        "Tape": "氛围",
    }
    return labels.get(category, "细节")


def display_signal(term):
    labels = {
        "warm": "温暖",
        "soft": "柔和",
        "quiet": "安静",
        "calm": "平静",
        "cozy": "松弛",
        "minimal": "克制",
        "minimalist": "极简",
        "texture": "纹理",
        "paper": "纸感",
        "wooden": "木质",
        "craft": "手作",
        "handmade": "手作",
        "vintage": "复古",
        "still life": "静物",
        "home": "居家",
        "desk": "桌面",
        "detail": "细节",
        "natural light": "自然光",
        "muted": "低饱和",
        "neutral": "中性色",
        "simple": "简单",
        "tactile": "触感",
        "product": "产品",
        "object": "物件",
        "material": "材质",
        "packaging": "包装",
    }
    return labels.get(term, term)


def extract_object_hint(alt):
    if not alt:
        return ""
    text = alt.strip().rstrip(".")
    if len(text) <= 28:
        return text
    for marker in [" featuring ", " of ", " with "]:
        if marker in text:
            tail = text.split(marker, 1)[1].strip()
            if 6 <= len(tail) <= 36:
                return tail
    return ""


def get_daily_keyword(keywords_cfg):
    """根据今天是星期几，从轮换列表取关键词 + 分类"""
    day_idx = time.localtime().tm_wday  # 0=周一, 6=周日
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    today_name = day_names[day_idx]
    rotation = keywords_cfg.get("daily_rotation", {})
    if today_name in rotation:
        return rotation[today_name]["keyword"], rotation[today_name]["category"]
    # 兜底：取第一个
    first = list(rotation.values())[0]
    return first["keyword"], first["category"]


def get_feishu_token(feishu_creds):
    """获取飞书 tenant_access_token"""
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": feishu_creds["app_id"], "app_secret": feishu_creds["app_secret"]}
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取 Token 失败: {data}")
    return data["tenant_access_token"]


def search_pexels(keyword, count, pexels_cfg):
    """搜索 Pexels 图片"""
    resp = requests.get(
        pexels_cfg["base_url"],
        params={"query": keyword, "per_page": count, "orientation": pexels_cfg["orientation"]},
        headers={"Authorization": pexels_cfg["api_key"]}
    )
    return resp.json().get("photos", [])


def get_existing_links(token, feishu_creds):
    """查飞书表格已有记录的 Source Link，用于去重"""
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records",
        params={"page_size": 500},
        headers={"Authorization": f"Bearer {token}"}
    )
    items = resp.json().get("data", {}).get("items", [])
    links = set()
    for item in items:
        field = item.get("fields", {}).get("Source Link｜来源链接", "")
        if isinstance(field, dict):
            links.add(field.get("link", ""))
        elif isinstance(field, str):
            links.add(field)
    return links, len(items)


def download_image(url, idx):
    """下载图片到临时文件"""
    resp = requests.get(url, stream=True, timeout=30)
    if resp.status_code != 200:
        return None, 0
    path = os.path.join(tempfile.gettempdir(), f"yono_pexels_{idx}.jpg")
    with open(path, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    size = os.path.getsize(path)
    return path, size


def upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds):
    """上传图片到飞书 Drive → 返回 file_token"""
    with open(img_path, "rb") as f:
        resp = requests.post(
            feishu_creds.get("upload_endpoint", "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"),
            headers={"Authorization": f"Bearer {token}"},
            data={
                "parent_type": feishu_creds.get("upload_parent_type", "bitable_file"),
                "parent_node": feishu_creds["app_token"],
                "file_name": filename,
                "size": str(img_size)
            },
            files={"file": (filename, f, "image/jpeg")}
        )
    result = resp.json()
    if result.get("code") != 0:
        print(f"  ⚠ 上传失败: {result.get('msg')}")
        return None
    return result.get("data", {}).get("file_token")


def write_records_to_feishu(token, records, feishu_creds):
    """批量写入飞书表格"""
    resp = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_create",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"records": records}
    )
    return resp.json()


def run(keyword=None, count=5, dry_run=False):
    """主流程：搜图 → 下载 → 上传 → OS.md标准评判 → 写入飞书"""
    # 加载配置
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)
    pexels_cfg = get_pexels_config(sources_cfg)

    token = get_feishu_token(feishu_creds)
    print(f"✅ Token 获取成功")

    # 选择关键词 + OS.md 标准自动评判
    if keyword is None:
        keyword, day_category = get_daily_keyword(keywords_cfg)
    else:
        day_category = None

    base_judge_fields = get_judge_fields(keyword, keywords_cfg, judge_cfg, category=day_category)
    category = base_judge_fields["Category｜分类"]
    print(f"🔍 关键词: {keyword} → 分类: {category}")
    print(f"   基础评分: {base_judge_fields['Score｜评分']} | 每张图片将独立修正")
    print(f"   YONO: {base_judge_fields['YONO Reason｜为什么适合 YONO']}")
    print(f"   视觉: {base_judge_fields['Visual Tags｜视觉标签']}")
    print(f"   情绪: {base_judge_fields['Mood Tags｜情绪标签']}")

    # 搜图
    photos = search_pexels(keyword, count, pexels_cfg)
    print(f"📸 Pexels 找到 {len(photos)} 张")

    # 查已有
    existing_links, existing_count = get_existing_links(token, feishu_creds)
    print(f"📋 飞书已有 {existing_count} 条, {len(existing_links)} 个唯一链接")

    if dry_run:
        print("🧪 Dry run: 只预览评分，不下载、不上传、不写入飞书")

    # 处理每张图
    now_ts = int(time.time())
    today_str = time.strftime("%Y-%m-%d")
    records = []
    success_count = 0

    for i, p in enumerate(photos):
        source_url = p.get("url", "")
        if source_url in existing_links:
            print(f"  ⏭ Photo {i}: 已存在，跳过")
            continue

        title = (p.get("alt") or "")[:20]
        photographer = p.get("photographer", "Unknown")
        asset_name = f"{today_str}_Pexels_{photographer}_{title}"
        judge_fields = get_judge_fields(keyword, keywords_cfg, judge_cfg, category=day_category, photo=p)

        if dry_run:
            print(
                f"  🧪 Photo {i}: {asset_name} "
                f"[{category} ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}]"
            )
            print(f"     {judge_fields['YONO Reason｜为什么适合 YONO']}")
            if judge_fields.get("标题"):
                print(f"     标题：{judge_fields['标题']}")
            if judge_fields.get("Content Angle｜内容角度"):
                print(f"     内容角度：{judge_fields['Content Angle｜内容角度']}")
            success_count += 1
            continue

        # 下载图片 (按配置的尺寸)
        img_url = p["src"][pexels_cfg["image_size"]]
        img_path, img_size = download_image(img_url, i)
        if img_path is None:
            print(f"  ❌ Photo {i}: 下载失败")
            continue

        # 上传到飞书 Drive
        filename = f"pexels_{today_str}_{i}.jpg"
        file_token = upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds)
        os.remove(img_path)  # 清理临时文件
        # 构造记录（合并 OS.md 评判字段 + 图片）
        record = {
            "fields": {
                "Asset Name｜素材名称": asset_name,
                "Date｜收集日期": now_ts,
                "Source｜来源平台": "Pexels",
                "Source Link｜来源链接": {"link": source_url, "text": source_url},
                **judge_fields,  # ← OS.md 标准自动评判字段
            }
        }
        if file_token:
            record["fields"]["Image / File｜图片或文件"] = [{"file_token": file_token}]

        records.append(record)
        has_img = "🖼" if file_token else "❌无图"
        print(
            f"  {has_img} Photo {i}: {asset_name} ({img_size/1024:.0f}KB) "
            f"[{category} ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}]"
        )
        success_count += 1

    # 写入飞书
    if dry_run:
        print(f"\n🧪 Dry run 完成，预览 {success_count} 条候选记录，未写入飞书")
        return success_count

    if records:
        result = write_records_to_feishu(token, records, feishu_creds)
        if result.get("code") == 0:
            created = result.get("data", {}).get("records", [])
            print(f"\n🎉 成功写入 {len(created)} 条记录!")
            img_count = sum(1 for r in created if r.get("fields", {}).get("Image / File｜图片或文件"))
            print(f"   其中 {img_count} 条含图片附件")
            print(f"   所有记录分类: {category} | 已按每张图片独立评分")
            return len(created)
        else:
            print(f"\n❌ 写入失败: {result.get('msg')}")
            return 0
    else:
        print("\n📋 无新记录（全部重复或搜索为空）")
        return 0


def backfill_records():
    """回填飞书表格中缺少评判字段的已有记录"""
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)

    token = get_feishu_token(feishu_creds)
    print(f"✅ Token 获取成功")

    # 获取所有记录
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records",
        params={"page_size": 500},
        headers={"Authorization": f"Bearer {token}"}
    )
    items = resp.json().get("data", {}).get("items", [])
    print(f"📋 总共 {len(items)} 条记录")

    modules = judge_cfg.get("modules", {})

    # 需要回填的字段列表
    FIELDS_TO_BACKFILL = [
        "Category｜分类", "Status｜状态", "Score｜评分",
        "YONO Reason｜为什么适合 YONO",
        "Visual Tags｜视觉标签", "Mood Tags｜情绪标签"
    ]

    updates = []
    for item in items:
        fields = item.get("fields", {})
        # 检查是否有空字段需要回填
        needs_update = False
        for f in FIELDS_TO_BACKFILL:
            if not fields.get(f):
                needs_update = True
                break

        if not needs_update:
            continue

        # 从 Asset Name 和 Source Link 推断关键词
        name = fields.get("Asset Name｜素材名称", "")
        link_field = fields.get("Source Link｜来源链接", "")
        if isinstance(link_field, dict):
            link = link_field.get("link", "") or link_field.get("text", "")
        elif isinstance(link_field, str):
            link = link_field
        else:
            link = ""

        # 如果已有 Category，用它；否则从名字推断
        existing_cat = fields.get("Category｜分类")
        if existing_cat:
            category = existing_cat
        else:
            category = classify_category(name + " " + link, keywords_cfg)

        # 生成评判字段
        judge = modules.get(category, modules.get("Tape", {}))
        default_status = judge_cfg.get("default_status", "Raw｜未判断")

        update_fields = {}
        for f in FIELDS_TO_BACKFILL:
            if not fields.get(f):
                if f == "Score｜评分":
                    update_fields[f] = judge.get("score", 3)
                elif f == "Status｜状态":
                    update_fields[f] = default_status
                elif f == "Category｜分类":
                    update_fields[f] = category
                elif f == "YONO Reason｜为什么适合 YONO":
                    curiosity_point = build_curiosity_point(category, name, [], [])
                    update_fields[f] = (
                        f"筛选理由：这条素材需要补充人工复核。 "
                        f"{judge.get('yono_reason', '')} "
                        f"好奇心点：{curiosity_point}"
                    )
                elif f == "Visual Tags｜视觉标签":
                    update_fields[f] = judge.get("visual_tags", [])
                elif f == "Mood Tags｜情绪标签":
                    update_fields[f] = judge.get("mood_tags", [])

        if update_fields:
            updates.append({
                "record_id": item["record_id"],
                "fields": update_fields
            })
            print(f"  {item['record_id']} | {name[:40]} → 补填 {list(update_fields.keys())}")

    if updates:
        # 分批更新（每批 5 条）
        total_updated = 0
        for batch_start in range(0, len(updates), 5):
            batch = updates[batch_start:batch_start+5]
            result = requests.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"records": batch}
            )
            r = result.json()
            if r.get("code") == 0:
                total_updated += len(batch)
                print(f"  Batch {batch_start//5+1}: ✅ {len(batch)} 条")
            else:
                print(f"  Batch {batch_start//5+1}: ❌ {r.get('msg')}")
        print(f"\n✅ 回填 {total_updated} 条记录!")
    else:
        print("所有记录已完整，无需回填")


def list_records():
    """查看飞书表格现有记录"""
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)

    token = get_feishu_token(feishu_creds)
    links, count = get_existing_links(token, feishu_creds)
    print(f"飞书表格现有 {count} 条记录")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YONO Collector: Pexels → 飞书（OS.md 标准）")
    parser.add_argument("--keyword", "-k", help="搜索关键词（默认按星期轮换）")
    parser.add_argument("--count", "-n", type=int, default=5, help="每次搜图数量（默认5）")
    parser.add_argument("--list", "-l", action="store_true", help="只查看飞书记录数")
    parser.add_argument("--backfill", "-b", action="store_true", help="回填已有记录的空评判字段")
    parser.add_argument("--dry-run", action="store_true", help="只预览评分，不下载、不上传、不写入飞书")
    args = parser.parse_args()

    if args.list:
        list_records()
    elif args.backfill:
        backfill_records()
    else:
        run(keyword=args.keyword, count=args.count, dry_run=args.dry_run)
