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


def get_judge_fields(keyword, keywords_cfg, judge_cfg, category=None):
    """根据关键词 + 分类，生成 OS.md 标准的评判字段"""
    if category is None:
        category = classify_category(keyword, keywords_cfg)

    modules = judge_cfg.get("modules", {})
    judge = modules.get(category, modules.get("Tape", {}))
    default_status = judge_cfg.get("default_status", "Raw｜未判断")

    return {
        "Category｜分类": category,
        "Status｜状态": default_status,
        "Score｜评分": judge["score"],
        "YONO Reason｜为什么适合 YONO": judge["yono_reason"],
        "Content Angle｜内容角度": judge["content_angle"],
        "Visual Tags｜视觉标签": judge["visual_tags"],
        "Mood Tags｜情绪标签": judge["mood_tags"],
    }


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


def run(keyword=None, count=5):
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

    judge_fields = get_judge_fields(keyword, keywords_cfg, judge_cfg, category=day_category)
    category = judge_fields["Category｜分类"]
    print(f"🔍 关键词: {keyword} → 分类: {category}")
    print(f"   评分: {judge_fields['Score｜评分']} | 状态: {judge_fields['Status｜状态']}")
    print(f"   YONO: {judge_fields['YONO Reason｜为什么适合 YONO']}")
    print(f"   视觉: {judge_fields['Visual Tags｜视觉标签']}")
    print(f"   情绪: {judge_fields['Mood Tags｜情绪标签']}")

    # 搜图
    photos = search_pexels(keyword, count, pexels_cfg)
    print(f"📸 Pexels 找到 {len(photos)} 张")

    # 查已有
    existing_links, existing_count = get_existing_links(token, feishu_creds)
    print(f"📋 飞书已有 {existing_count} 条, {len(existing_links)} 个唯一链接")

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
        title = (p.get("alt") or "")[:20]
        photographer = p.get("photographer", "Unknown")
        asset_name = f"{today_str}_Pexels_{photographer}_{title}"

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
        print(f"  {has_img} Photo {i}: {asset_name} ({img_size/1024:.0f}KB) [{category} ★{judge_fields['Score｜评分']}]")
        success_count += 1

    # 写入飞书
    if records:
        result = write_records_to_feishu(token, records, feishu_creds)
        if result.get("code") == 0:
            created = result.get("data", {}).get("records", [])
            print(f"\n🎉 成功写入 {len(created)} 条记录!")
            img_count = sum(1 for r in created if r.get("fields", {}).get("Image / File｜图片或文件"))
            print(f"   其中 {img_count} 条含图片附件")
            print(f"   所有记录分类: {category} | 评分: ★{judge_fields['Score｜评分']}")
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
        "YONO Reason｜为什么适合 YONO", "Content Angle｜内容角度",
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
                    update_fields[f] = judge.get("yono_reason", "")
                elif f == "Content Angle｜内容角度":
                    update_fields[f] = judge.get("content_angle", "")
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
    args = parser.parse_args()

    if args.list:
        list_records()
    elif args.backfill:
        backfill_records()
    else:
        run(keyword=args.keyword, count=args.count)
