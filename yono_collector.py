#!/usr/bin/env python3
"""
YONO Collector — Pexels → 飞书 Asset 表格
按 OS.md (YONO Playbook v1.0) 标准自动分类、评分、标注

配置全部从 config/ 目录的 YAML 文件读取：
  config/keywords.yaml  → 搜索关键词、分类映射、模糊匹配规则
  config/judge.yaml     → 六大模块的评分、原因、标签标准
  config/sources.yaml   → Pexels / Music 来源、飞书连接信息

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
import mimetypes
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


def get_music_config(sources_cfg):
    """从配置获取音乐搜索参数。默认使用无需密钥的 iTunes Search API。"""
    music = sources_cfg.get("music", {})
    return {
        "provider": music.get("provider", "itunes"),
        "base_url": music.get("base_url", "https://itunes.apple.com/search"),
        "country": music.get("country", "US"),
        "media": music.get("media", "music"),
        "entity": music.get("entity", "song"),
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
        "Title｜标题": quality["title"],
        **({"Post Content｜发布内容": quality["post_content"]} if quality["post_content"] else {}),
        "Content Angle｜内容角度": quality["content_angle"],
        "Visual Tags｜视觉标签": judge["visual_tags"],
        "Mood Tags｜情绪标签": judge["mood_tags"],
    }


def get_music_judge_fields(keyword, keywords_cfg, judge_cfg, track, category="Tape"):
    """根据音乐元数据生成 Tape 标准字段。"""
    modules = judge_cfg.get("modules", {})
    judge = modules.get(category, modules.get("Tape", {}))
    default_status = judge_cfg.get("default_status", "Raw｜未判断")
    quality = evaluate_music_quality(keyword, track, judge, judge_cfg)

    return {
        "Category｜分类": category,
        "Status｜状态": quality["status"] or default_status,
        "Score｜评分": quality["score"],
        "YONO Reason｜为什么适合 YONO": quality["reason"],
        "Title｜标题": quality["title"],
        **({"Post Content｜发布内容": quality["post_content"]} if quality["post_content"] else {}),
        "Content Angle｜内容角度": quality["content_angle"],
        "Visual Tags｜视觉标签": judge["visual_tags"],
        "Mood Tags｜情绪标签": judge["mood_tags"],
    }


def build_asset_name(category, alt="", visual_tags=None, reason=""):
    """生成 7 字以内、偏文艺、能提示使用方向的素材名。"""
    visual_tags = visual_tags or []
    text = normalize_text(" ".join([alt, reason, " ".join(map(str, visual_tags))]))

    object_names = [
        (["penholder", "pen holder", "笔筒"], "笔筒"),
        (["claylamp", "lamp", "台灯", "灯"], "灯下"),
        (["deskcorner", "desk corner", "桌角"], "桌角"),
        (["workmoment", "workspace", "work space", "laptop", "工作"], "桌上"),
        (["notebooktag", "notebook", "tag", "腰封", "笔记本"], "纸签"),
        (["sneaker", "shoe", "shoes", "白鞋", "鞋"], "白鞋"),
        (["ceramic", "vase", "clay", "陶", "花瓶"], "陶器"),
        (["room", "interior", "home", "屋", "室内"], "屋内"),
        (["vintage", "classic", "archive", "复古"], "旧影"),
        (["texture", "material", "wood", "paper", "材质", "纹理"], "材质"),
        (["packaging", "package", "包装"], "小礼"),
    ]
    object_anchor = ""
    for needles, label in object_names:
        if any(needle in text for needle in needles):
            object_anchor = label
            break

    signal_names = [
        (["warm light", "warm", "sunlight", "灯", "光"], "暖光"),
        (["paper texture", "paper", "纸", "便签", "notebook"], "纸上"),
        (["wooden texture", "wood", "wooden", "木"], "木纹"),
        (["ceramic", "clay", "陶"], "陶色"),
        (["vintage", "classic", "old", "复古"], "旧影"),
        (["desk", "workspace", "桌"], "桌角"),
        (["home", "room", "interior", "居家"], "屋内"),
        (["soft color", "neutral", "muted"], "柔色"),
        (["sneaker", "shoes", "outfit"], "日常"),
        (["packaging", "gift"], "小礼"),
        (["music", "record", "vinyl", "tape"], "声纹"),
        (["texture", "material", "craft", "handmade"], "质感"),
    ]

    anchor = object_anchor
    for needles, label in signal_names:
        if anchor:
            break
        if any(needle in text for needle in needles):
            anchor = label
            break

    if not anchor:
        anchor = {
            "Postcard": "情绪",
            "Archive": "旧影",
            "Field Notes": "观察",
            "OOTD": "日常",
            "Block": "质感",
            "Tape": "声纹",
        }.get(category, "片刻")

    suffix = {
        "Postcard": "片刻",
        "Archive": "档案",
        "Field Notes": "札记",
        "OOTD": "姿态",
        "Block": "小物",
        "Tape": "余音",
    }.get(category, "素材")

    name = f"{anchor}{suffix}"
    return name[:7]


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
            "post_content": "",
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
    curiosity_point = build_curiosity_point(category, alt, positive_hits, category_hits)
    reason = build_selection_reason(
        judge.get("yono_reason", ""),
        evidence,
        score,
        curiosity_point
    )
    # 内容角度：始终生成，帮助判断
    content_angle = build_xiaohongshu_content_angle(category, alt, reason, curiosity_point)
    # 新建记录状态统一 Raw｜未判断，由用户手动改为 Save 后再生成 Title 和发布内容
    return {
        "score": score,
        "status": "Raw｜未判断",
        "reason": reason,
        "title": "",
        "content_angle": content_angle,
        "post_content": "",
    }


def evaluate_music_quality(keyword, track, judge, judge_cfg):
    """按单首歌的元数据独立评分。"""
    model = judge_cfg.get("quality_model", {})
    base_score = int(judge.get("score", 3))
    track_name = track.get("trackName", "")
    artist = track.get("artistName", "")
    album = track.get("collectionName", "")
    genre = track.get("primaryGenreName", "")
    text = normalize_text(" ".join([keyword or "", track_name, artist, album, genre]))

    positive_terms = model.get("music_positive_signals", [
        "ambient", "indie", "acoustic", "piano", "jazz", "soundtrack",
        "folk", "soul", "chill", "dream", "quiet", "soft", "calm",
        "instrumental", "lofi", "classic", "cinematic",
    ])
    anti_terms = model.get("music_anti_signals", [
        "explicit", "karaoke", "workout", "party", "dance remix",
        "club", "christmas", "kids", "comedy",
    ])

    positive_hits = matched_terms(text, positive_terms)
    anti_hits = matched_terms(text, anti_terms)

    score = base_score
    evidence = []

    if track_name and artist:
        evidence.append(f"歌曲：{track_name} — {artist}")
    else:
        score -= 1
        evidence.append("歌曲或艺人信息不完整")

    if album:
        evidence.append(f"专辑：{album}")
    if genre:
        evidence.append(f"类型：{genre}")

    if track.get("previewUrl"):
        score += 1
        evidence.append("有试听片段，适合快速判断是否收藏")
    else:
        score -= 1
        evidence.append("缺少试听片段")

    if track.get("artworkUrl100"):
        score += 1
        evidence.append("有封面，可直接用于素材库识别")

    if positive_hits:
        score += 1
        evidence.append(f"Tape 信号：{', '.join(positive_hits[:4])}")
    if anti_hits:
        score -= min(2, len(anti_hits))
        evidence.append(f"反向信号：{', '.join(anti_hits[:3])}")

    if not track.get("previewUrl"):
        score = min(score, 3)
    if anti_hits:
        score = min(score, 3)

    score = max(1, min(5, score))
    curiosity_point = build_music_curiosity_point(track, positive_hits)
    reason = build_selection_reason(
        judge.get("yono_reason", ""),
        evidence,
        score,
        curiosity_point,
        subject="这首歌",
    )
    # 内容角度始终生成
    content_angle = build_music_content_angle(track, reason, curiosity_point)
    # 新建记录状态统一 Raw｜未判断
    return {
        "score": score,
        "status": "Raw｜未判断",
        "reason": reason,
        "title": "",
        "content_angle": content_angle,
        "post_content": "",
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


def build_selection_reason(base_reason, evidence, score, curiosity_point, subject="这张图"):
    verdict = {
        5: f"筛选理由：{subject}高度值得保留。",
        4: f"筛选理由：{subject}适合进入素材库。",
        3: f"筛选理由：{subject}有可用价值，但需要人工复核。",
        2: f"筛选理由：{subject} YONO 信号偏弱，暂不建议保留。",
        1: f"筛选理由：{subject}不符合当前筛选标准。",
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
        "OOTD": f"这张图里的{anchor}，藏着一种生活方式信号",
        "Block": f"这个{anchor}细节，才是照片真正耐看的地方",
        "Tape": f"这张图有种{anchor}的安静后劲",
    }
    title = templates.get(category, f"这张图的{anchor}感，值得存进素材库")

    # 如果图片描述里有更具体的物件，补一点画面感，但控制长度。
    object_hint = extract_object_hint(alt)
    if object_hint and object_hint.lower() not in title.lower():
        title = f"{title}｜{object_hint}"

    return title[:60]


def build_music_curiosity_point(track, positive_hits):
    track_name = track.get("trackName", "这首歌")
    artist = track.get("artistName", "")
    genre = track.get("primaryGenreName", "")
    signal = display_signal(positive_hits[0]) if positive_hits else "情绪后劲"
    artist_text = f"{artist} 的" if artist else ""
    genre_text = f"，它被归在 {genre}，但听感可能比类型标签更细" if genre else ""
    return f"{artist_text}{track_name} 的好奇心点在于：它不是只提供背景声，而是带着{signal}线索{genre_text}。"


def build_music_title(track, positive_hits):
    track_name = track.get("trackName", "这首歌")
    artist = track.get("artistName", "")
    signal = display_signal(positive_hits[0]) if positive_hits else "安静后劲"
    if artist:
        title = f"今天想存下 {artist} 的这首歌：有种{signal}"
    else:
        title = f"今天想存下这首歌：有种{signal}"
    if track_name and len(title) < 48:
        title = f"{title}｜{track_name}"
    return title[:60]


def build_music_content_angle(track, reason, curiosity_point):
    track_name = track.get("trackName", "这首歌")
    artist = track.get("artistName", "")
    album = track.get("collectionName", "")
    genre = track.get("primaryGenreName", "")
    subject = f"{track_name} — {artist}" if artist else track_name
    album_text = f" 来自《{album}》。" if album else ""
    genre_text = f" 类型标签是 {genre}，" if genre else ""
    return (
        f"音乐分享切入：把 {subject} 当作一条可收藏的声音素材。"
        f"{album_text}{genre_text}重点不是介绍歌曲资料，而是写它适合出现的生活场景、情绪温度和画面感。"
        f" {curiosity_point} {reason[:120]}"
    )


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


def build_xiaohongshu_post(category, title, alt, curiosity_point, positive_hits, category_hits, reason):
    """生成完整的小红书正文 + 标签，仅 Save 素材调用。"""
    # 开头钩子 — 用好奇心点改写成疑问或反差句
    hook_templates = {
        "Postcard":    "有些图，不知道为什么，就是让人想存下来。",
        "Archive":     "这个东西存在了很久，但今天才第一次觉得它值得被认真看一眼。",
        "Field Notes": "日常里总有一些细节，你不记录就会忘掉。",
        "OOTD":        "有时候一件东西的存在方式，比它本身更耐看。",
        "Block":       "不是所有好东西都需要很贵，但它们都需要被认真对待。",
        "Tape":        "有些声音不适合放进歌单，更适合放进生活里。",
    }
    hook = hook_templates.get(category, "有些东西值得被好好看一眼。")

    # 主体 — 从 alt 描述和分类角度展开
    body_templates = {
        "Postcard":    "把它存下来不是因为它有多特别，而是因为它刚好说出了某个你说不清楚的感受。",
        "Archive":     "设计到这个程度，已经不只是好看，而是有态度、有立场、有时间感。",
        "Field Notes": "这种场景每天都在发生，但大多数时候我们都走过去了，没有停下来想。",
        "OOTD":        "穿搭从来不只是穿什么，而是你怎么把东西和生活放在一起。",
        "Block":       "材质、细节、结构——这些东西才是真正决定一件物品值不值得拥有的东西。",
        "Tape":        "不是每首歌都适合大声放，有些歌更适合用来填满某个安静的下午。",
    }
    body = body_templates.get(category, "YONO 原则：只要值得，就收下。")

    # 好奇心点段落
    curiosity_para = curiosity_point if curiosity_point else ""

    # 结尾 CTA
    cta_templates = {
        "Postcard":    "你有没有也存了某张说不清楚为什么喜欢的图？",
        "Archive":     "这种东西你会存进哪个收藏夹？",
        "Field Notes": "今天你注意到了什么？",
        "OOTD":        "你最近有没有找到一件特别对的东西？",
        "Block":       "你有没有一个只放好东西的收藏夹？",
        "Tape":        "你有没有一首歌，不放进歌单，但总在某个时刻想起来？",
    }
    cta = cta_templates.get(category, "你会把它存下来吗？")

    # 标签 — 分类固定标签 + 信号词标签
    base_tags = {
        "Postcard":    ["#情绪收藏", "#YONO", "#值得收藏", "#生活美学"],
        "Archive":     ["#设计档案", "#YONO", "#值得保存", "#好设计"],
        "Field Notes": ["#日常观察", "#YONO", "#生活灵感", "#品牌思考"],
        "OOTD":        ["#穿搭日记", "#YONO", "#生活方式", "#人与物"],
        "Block":       ["#材质细节", "#YONO", "#好物", "#设计商品"],
        "Tape":        ["#音乐分享", "#YONO", "#歌单", "#声音美学"],
    }.get(category, ["#YONO", "#收藏"])

    signal_tags = [f"#{display_signal(t)}" for t in (positive_hits + category_hits)[:3] if display_signal(t) != t]
    seen = set(base_tags)
    dedup_signal = [t for t in signal_tags if t not in seen and not seen.add(t)]
    all_tags = base_tags + dedup_signal

    parts = [hook, "", body]
    if curiosity_para:
        parts += ["", curiosity_para]
    parts += ["", cta, "", " ".join(all_tags)]
    return "\n".join(parts)


def build_xiaohongshu_music_post(track, title, curiosity_point, positive_hits, reason):
    """生成音乐类（Tape）完整小红书正文 + 标签。"""
    track_name = track.get("trackName", "这首歌")
    artist = track.get("artistName", "")
    album = track.get("collectionName", "")
    genre = track.get("primaryGenreName", "")

    subject = f"{track_name}（{artist}）" if artist else track_name
    hook = f"今天想认真分享一首歌：{subject}。"

    body_parts = []
    if album:
        body_parts.append(f"它来自《{album}》。")
    if genre:
        body_parts.append(f"类型标签是 {genre}，但听感比这个词更细腻。")
    body_parts.append("不是为了推荐，只是觉得它值得被认真听一次。")
    body = " ".join(body_parts)

    curiosity_para = curiosity_point if curiosity_point else ""

    signal_words = [display_signal(t) for t in positive_hits[:2] if display_signal(t) != t]
    mood_desc = "、".join(signal_words) if signal_words else "安静后劲"
    cta = f"如果你也喜欢带着{mood_desc}的音乐，可以存下来慢慢听。"

    base_tags = ["#音乐分享", "#YONO", "#歌单推荐", "#声音美学", "#Tape"]
    signal_tags = [f"#{display_signal(t)}" for t in positive_hits[:3] if display_signal(t) != t]
    all_tags = base_tags + [t for t in signal_tags if t not in base_tags]

    parts = [hook, "", body]
    if curiosity_para:
        parts += ["", curiosity_para]
    parts += ["", cta, "", " ".join(all_tags)]
    return "\n".join(parts)


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
        "warm light": "暖光",
        "soft color": "柔和配色",
        "playful object": "有趣物件",
        "everyday scene": "日常场景",
        "vintage feeling": "复古感",
        "wooden texture": "木质纹理",
        "paper texture": "纸张纹理",
        "small gift": "小礼物感",
        "childlike detail": "童趣细节",
        "quiet home": "安静居家",
        "desk object": "桌面物件",
        "packaging detail": "包装细节",
        "handmade feeling": "手作感",
        "ambient": "氛围",
        "indie": "独立感",
        "acoustic": "原声",
        "piano": "钢琴",
        "jazz": "爵士",
        "soundtrack": "电影感",
        "folk": "民谣",
        "soul": "灵魂乐",
        "chill": "松弛",
        "dream": "梦感",
        "instrumental": "器乐",
        "lofi": "低保真",
        "cinematic": "电影感",
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


def get_unsplash_config(sources_cfg):
    return sources_cfg.get("unsplash", {})


def get_museum_config(sources_cfg):
    return sources_cfg.get("museums", {})


def search_museum(keyword, count, museum_cfg):
    """搜博物馆藏品。依次尝试所有已启用的博物馆，凑够 count 条为止。
    顺序：Met → AIC → V&A → Cleveland → Europeana
    统一格式：{title, artist, medium, department, image_url, source_url, source_name}
    """
    if not museum_cfg.get("enabled"):
        return []

    results = []
    need = count  # 每家只搜够剩余数量，节省请求数

    searchers = [
        ("met",       _search_met,       museum_cfg.get("met", {})),
        ("aic",       _search_aic,       museum_cfg.get("aic", {})),
        ("vam",       _search_vam,       museum_cfg.get("vam", {"enabled": True})),
        ("cleveland", _search_cleveland, museum_cfg.get("cleveland", {"enabled": True})),
        ("europeana", _search_europeana, museum_cfg.get("europeana", {"enabled": True})),
    ]

    for name, fn, cfg in searchers:
        if len(results) >= count:
            break
        if not cfg.get("enabled", True):
            continue
        got = fn(keyword, need * 3, cfg)
        results.extend(got)
        print(f"  🏛 {name}: 找到 {len(got)} 件")

    return results


def _search_met(keyword, limit, met_cfg):
    try:
        r = requests.get(
            met_cfg["search_url"],
            params={"q": keyword, "hasImages": True, "isPublicDomain": True},
            timeout=15,
        )
        ids = r.json().get("objectIDs") or []
        items = []
        for oid in ids[:limit]:
            try:
                obj = requests.get(f"{met_cfg['object_url']}/{oid}", timeout=10).json()
            except Exception:
                continue
            img = obj.get("primaryImage", "")
            if not img:
                continue
            items.append({
                "title": obj.get("title", ""),
                "artist": obj.get("artistDisplayName", ""),
                "medium": obj.get("medium", ""),
                "department": obj.get("department", ""),
                "image_url": img,
                "source_url": obj.get("objectURL", ""),
                "source_name": "The Metropolitan Museum of Art",
            })
            if len(items) >= limit:
                break
        return items
    except Exception as e:
        print(f"  ⚠️ Met API 异常: {e}")
        return []


def _search_aic(keyword, limit, aic_cfg):
    try:
        r = requests.get(
            aic_cfg["search_url"],
            params={
                "q": keyword,
                "fields": "id,title,artist_display,image_id,api_link,medium_display,department_title",
                "limit": limit,
            },
            timeout=15,
        )
        hits = r.json().get("data", [])
        items = []
        base = aic_cfg.get("image_base_url", "https://www.artic.edu/iiif/2")
        for h in hits:
            if not h.get("image_id"):
                continue
            items.append({
                "title": h.get("title", ""),
                "artist": h.get("artist_display", "").split("\n")[0],
                "medium": h.get("medium_display", ""),
                "department": h.get("department_title", ""),
                "image_url": f"{base}/{h['image_id']}/full/843,/0/default.jpg",
                "source_url": h.get("api_link", "").replace("/api/v1/artworks/", "/artworks/"),
                "source_name": "Art Institute of Chicago",
            })
        return items
    except Exception as e:
        print(f"  ⚠️ AIC API 异常: {e}")
        return []


def _search_vam(keyword, limit, cfg):
    """Victoria and Albert Museum（英国）— 免费无需 API Key"""
    try:
        r = requests.get(
            "https://api.vam.ac.uk/v2/objects/search",
            params={"q": keyword, "images_exist": 1, "page_size": min(limit, 45)},
            timeout=15,
        )
        hits = r.json().get("records", [])
        items = []
        for h in hits:
            img_id = (h.get("_primaryImageId") or "").strip()
            if not img_id:
                continue
            items.append({
                "title": h.get("_primaryTitle", ""),
                "artist": h.get("_primaryMaker", {}).get("name", "") if isinstance(h.get("_primaryMaker"), dict) else "",
                "medium": "",
                "department": h.get("_primaryPlace", ""),
                "image_url": f"https://framemark.vam.ac.uk/collections/{img_id}/full/735,/0/default.jpg",
                "source_url": f"https://collections.vam.ac.uk/item/{h.get('systemNumber', '')}",
                "source_name": "Victoria and Albert Museum",
            })
        return items
    except Exception as e:
        print(f"  ⚠️ V&A API 异常: {e.__class__.__name__}")
        return []


def _search_cleveland(keyword, limit, cfg):
    """Cleveland Museum of Art（美国克利夫兰）— 免费无需 API Key"""
    try:
        r = requests.get(
            "https://openaccess-api.clevelandart.org/api/artworks/",
            params={"q": keyword, "has_image": 1, "limit": min(limit, 100)},
            timeout=15,
        )
        hits = r.json().get("data", [])
        items = []
        for h in hits:
            img = (h.get("images") or {}).get("web", {}).get("url", "")
            if not img:
                continue
            items.append({
                "title": h.get("title", ""),
                "artist": ", ".join(
                    a.get("description", "") for a in (h.get("creators") or [])[:1]
                ),
                "medium": h.get("technique", ""),
                "department": h.get("department", ""),
                "image_url": img,
                "source_url": h.get("url", ""),
                "source_name": "Cleveland Museum of Art",
            })
        return items
    except Exception as e:
        print(f"  ⚠️ Cleveland API 异常: {e.__class__.__name__}")
        return []


def _search_europeana(keyword, limit, cfg):
    """Europeana（欧洲文化遗产聚合平台）— 免费，需 API Key（申请地址在 config 里）"""
    api_key = cfg.get("api_key", "api2demo")  # api2demo 是公开演示 Key，有限速
    try:
        r = requests.get(
            "https://api.europeana.eu/record/v2/search.json",
            params={
                "wskey": api_key,
                "query": keyword,
                "qf": "TYPE:IMAGE",
                "media": "true",
                "rows": min(limit, 100),
                "profile": "rich",
            },
            timeout=15,
        )
        hits = r.json().get("items", [])
        items = []
        for h in hits:
            imgs = h.get("edmIsShownBy") or h.get("edmPreview") or []
            img = imgs[0] if imgs else ""
            if not img:
                continue
            title_list = h.get("title") or h.get("dcTitle") or [""]
            creator_list = h.get("dcCreator") or [""]
            items.append({
                "title": title_list[0] if title_list else "",
                "artist": creator_list[0] if creator_list else "",
                "medium": "",
                "department": (h.get("dataProvider") or [""])[0],
                "image_url": img,
                "source_url": h.get("guid", ""),
                "source_name": f"Europeana / {(h.get('dataProvider') or [''])[0]}",
            })
        return items
    except Exception as e:
        print(f"  ⚠️ Europeana API 异常: {e.__class__.__name__}")
        return []


def search_unsplash(keyword, count, unsplash_cfg):
    """搜索 Unsplash 图片，返回和 Pexels 兼容的格式。"""
    api_key = unsplash_cfg.get("api_key", "")
    if not api_key:
        return []
    try:
        resp = requests.get(
            unsplash_cfg.get("base_url", "https://api.unsplash.com/search/photos"),
            params={
                "query": keyword,
                "per_page": count,
                "orientation": unsplash_cfg.get("orientation", "squarish"),
            },
            headers={"Authorization": f"Client-ID {api_key}"},
            timeout=15,
        )
        results = resp.json().get("results", [])
        # 统一成 Pexels 格式方便复用
        photos = []
        for r in results:
            urls = r.get("urls", {})
            user = r.get("user", {})
            photos.append({
                "_source": "Unsplash",
                "url": r.get("links", {}).get("html", ""),
                "alt": r.get("alt_description") or r.get("description") or "",
                "width": r.get("width", 0),
                "height": r.get("height", 0),
                "photographer": user.get("name", ""),
                "src": {
                    "large": urls.get("regular", ""),
                    "original": urls.get("full", ""),
                },
            })
        return photos
    except Exception as e:
        print(f"  ⚠️ Unsplash API 异常: {e}")
        return []


def search_pexels(keyword, count, pexels_cfg):
    """搜索 Pexels 图片"""
    resp = requests.get(
        pexels_cfg["base_url"],
        params={"query": keyword, "per_page": count, "orientation": pexels_cfg["orientation"]},
        headers={"Authorization": pexels_cfg["api_key"]}
    )
    return resp.json().get("photos", [])


def search_music(keyword, count, music_cfg):
    """搜索音乐。默认使用 iTunes Search API。"""
    if music_cfg.get("provider") != "itunes":
        raise ValueError(f"暂不支持音乐来源: {music_cfg.get('provider')}")

    resp = requests.get(
        music_cfg["base_url"],
        params={
            "term": keyword,
            "country": music_cfg["country"],
            "media": music_cfg["media"],
            "entity": music_cfg["entity"],
            "limit": min(max(count * 5, count), 25),
        },
        timeout=30,
    )
    data = resp.json()
    return data.get("results", [])


def music_source_link(track):
    return track.get("trackViewUrl") or track.get("collectionViewUrl") or track.get("previewUrl") or ""


def music_artwork_url(track):
    url = track.get("artworkUrl100", "")
    return url.replace("100x100bb", "600x600bb") if url else ""


def build_music_asset_name(track):
    track_name = str(track.get("trackName") or "未命名歌曲").strip()
    artist = str(track.get("artistName") or "").strip()
    name = f"{track_name} - {artist}" if artist else track_name
    return name[:48]


def safe_filename(value):
    text = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value))
    return "_".join(text.split("_"))[:80] or "music"


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
    """下载图片到临时文件，网络异常时返回 (None, 0) 而非崩溃"""
    try:
        resp = requests.get(url, stream=True, timeout=30)
        if resp.status_code != 200:
            return None, 0
        path = os.path.join(tempfile.gettempdir(), f"yono_pexels_{idx}.jpg")
        with open(path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        size = os.path.getsize(path)
        return path, size
    except Exception as e:
        print(f"  ⚠️ 图片下载异常（跳过）: {e.__class__.__name__}: {str(e)[:80]}")
        return None, 0


def download_file(url, filename, headers=None, min_size=1, expected_content_prefix=None):
    """下载文件到临时目录，返回路径和大小。"""
    if not url:
        return None, 0
    resp = requests.get(url, stream=True, timeout=30, headers=headers or {})
    if resp.status_code != 200:
        if resp.status_code != 206:
            return None, 0
    content_type = resp.headers.get("content-type", "")
    if expected_content_prefix and not content_type.startswith(expected_content_prefix):
        return None, 0
    path = os.path.join(tempfile.gettempdir(), filename)
    with open(path, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    size = os.path.getsize(path)
    if size < min_size:
        os.remove(path)
        return None, 0
    return path, size


def upload_to_feishu_drive(token, file_path, file_size, filename, feishu_creds, mime_type=None):
    """上传文件到飞书 Drive → 返回 file_token。"""
    mime_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        resp = requests.post(
            feishu_creds.get("upload_endpoint", "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"),
            headers={"Authorization": f"Bearer {token}"},
            data={
                "parent_type": feishu_creds.get("upload_parent_type", "bitable_file"),
                "parent_node": feishu_creds["app_token"],
                "file_name": filename,
                "size": str(file_size)
            },
            files={"file": (filename, f, mime_type)}
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


def _get_photo_url(src_type, item, pexels_cfg):
    """从不同来源的 photo/art 对象提取可下载的图片 URL。"""
    if src_type == "museum":
        return item.get("image_url", "")
    if src_type == "unsplash":
        return item["src"].get("large") or item["src"].get("original", "")
    if src_type == "pexels":
        return item["src"].get(pexels_cfg.get("image_size", "large"), "")
    return ""


def _get_photo_source_url(src_type, item):
    """从 photo/art 对象提取来源页面 URL（用于去重）。"""
    if src_type == "museum":
        return item.get("source_url", "")
    return item.get("url", "")


def run(keyword=None, count=5, images_per_record=4, dry_run=False):
    """主流程：Tape 搜音乐，其它分类搜图 → 写入飞书。
    每条图片记录附 images_per_record 张图（3-5，默认4）。
    """
    images_per_record = max(1, min(5, images_per_record))

    # 加载配置
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)
    pexels_cfg = get_pexels_config(sources_cfg)
    music_cfg = get_music_config(sources_cfg)
    museum_cfg = get_museum_config(sources_cfg)
    unsplash_cfg = get_unsplash_config(sources_cfg)

    token = get_feishu_token(feishu_creds)
    print(f"✅ Token 获取成功")

    # 选择关键词 + OS.md 标准自动评判
    if keyword is None:
        keyword, day_category = get_daily_keyword(keywords_cfg)
    else:
        day_category = None

    base_judge_fields = get_judge_fields(keyword, keywords_cfg, judge_cfg, category=day_category)
    category = base_judge_fields["Category｜分类"]

    # Block 和 OOTD 由用户手动上传真实图片，不支持自动抓取
    if category in ("Block", "OOTD"):
        print(f"⛔ 分类 {category} 使用用户上传的真实图片，不支持自动抓取。")
        print(f"   请手动上传图片到飞书，或换一个 Postcard / Archive / Field Notes / Tape 关键词。")
        return 0

    print(f"🔍 关键词: {keyword} → 分类: {category}  每条记录 {images_per_record} 张图")
    item_label = "每首音乐" if category == "Tape" else "每张图片"
    print(f"   基础评分: {base_judge_fields['Score｜评分']} | {item_label}将独立修正")
    print(f"   YONO: {base_judge_fields['YONO Reason｜为什么适合 YONO']}")
    print(f"   视觉: {base_judge_fields['Visual Tags｜视觉标签']}")
    print(f"   情绪: {base_judge_fields['Mood Tags｜情绪标签']}")

    if category == "Tape":
        return run_music_collection(
            keyword,
            count,
            dry_run,
            token,
            feishu_creds,
            keywords_cfg,
            judge_cfg,
            music_cfg,
        )

    # 需要的图片总量：每条记录 images_per_record 张，多搜 50% 备用
    fetch_total = count * images_per_record * 2

    # 图源策略：
    #   Archive      → 博物馆优先，不足补 Pexels
    #   Postcard / Field Notes → Unsplash 优先（有 Key），不足补 Pexels
    #   其他         → Pexels
    all_candidates = []  # [(src_type, item), ...]

    if category == "Archive" and museum_cfg.get("enabled"):
        museum_items = search_museum(keyword, fetch_total, museum_cfg)
        print(f"🏛 博物馆找到 {len(museum_items)} 件藏品")
        for item in museum_items:
            all_candidates.append(("museum", item))

    if category in ("Postcard", "Field Notes") and unsplash_cfg.get("api_key"):
        unsplash_photos = search_unsplash(keyword, fetch_total, unsplash_cfg)
        print(f"🌿 Unsplash 找到 {len(unsplash_photos)} 张")
        for p in unsplash_photos:
            all_candidates.append(("unsplash", p))

    pexels_need = max(0, fetch_total - len(all_candidates))
    if pexels_need > 0:
        pexels_photos = search_pexels(keyword, pexels_need, pexels_cfg)
        if pexels_photos:
            src_label = "补充" if all_candidates else ""
            print(f"📸 Pexels 找到 {len(pexels_photos)} 张{'（' + src_label + '）' if src_label else ''}")
        for p in pexels_photos:
            all_candidates.append(("pexels", p))

    # 查已有，过滤重复
    existing_links, existing_count = get_existing_links(token, feishu_creds)
    print(f"📋 飞书已有 {existing_count} 条, {len(existing_links)} 个唯一链接")

    new_candidates = [
        (st, it) for st, it in all_candidates
        if _get_photo_source_url(st, it) not in existing_links
    ]
    print(f"   候选图片 {len(all_candidates)} 张，去重后 {len(new_candidates)} 张，每条记录 {images_per_record} 张")

    if dry_run:
        print("🧪 Dry run: 只预览评分，不下载、不上传、不写入飞书")

    now_ts = int(time.time())
    today_str = time.strftime("%Y-%m-%d")
    records = []
    success_count = 0
    idx = 0  # global photo index for unique filenames

    while success_count < count and idx < len(new_candidates):
        # 取本条记录所需的 images_per_record 张，主图 + 附图
        group = new_candidates[idx:idx + images_per_record]
        if not group:
            break
        idx += images_per_record

        primary_src, primary_item = group[0]

        # 用主图生成评判字段和素材名
        if primary_src == "museum":
            alt_text = " ".join(filter(None, [
                primary_item["title"], primary_item["artist"],
                primary_item["medium"], primary_item["department"]
            ]))
            pseudo_photo = {"alt": alt_text, "width": 2000, "height": 2000}
        else:
            pseudo_photo = primary_item
            alt_text = primary_item.get("alt") or ""

        judge_fields = get_judge_fields(keyword, keywords_cfg, judge_cfg, category=day_category, photo=pseudo_photo)
        asset_name = build_asset_name(
            judge_fields["Category｜分类"],
            alt=alt_text,
            visual_tags=judge_fields.get("Visual Tags｜视觉标签", []),
            reason=judge_fields.get("YONO Reason｜为什么适合 YONO", ""),
        )
        source_url = _get_photo_source_url(primary_src, primary_item)

        if dry_run:
            src_label = {"museum": "🏛", "unsplash": "🌿", "pexels": "📸"}.get(primary_src, "📷")
            print(
                f"  🧪 记录 {success_count+1}: {src_label} {asset_name} "
                f"[{category} ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}]"
                f"  ({len(group)} 张图)"
            )
            if primary_src == "museum":
                print(f"     来源: {primary_item.get('source_name','')} | {primary_item.get('title','')[:50]}")
            print(f"     {judge_fields['YONO Reason｜为什么适合 YONO'][:80]}")
            success_count += 1
            continue

        # 下载并上传所有图片
        file_tokens = []
        total_kb = 0
        for gi, (src_type, item) in enumerate(group):
            img_url = _get_photo_url(src_type, item, pexels_cfg)
            if not img_url:
                continue
            src_prefix = {"museum": "museum", "unsplash": "unsplash", "pexels": "pexels"}.get(src_type, "img")
            tmp_idx = success_count * images_per_record + gi
            img_path, img_size = download_image(img_url, f"{src_prefix}_{tmp_idx}")
            if img_path is None:
                continue
            filename = f"{src_prefix}_{today_str}_{tmp_idx}.jpg"
            ft = upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds)
            os.remove(img_path)
            if ft:
                file_tokens.append({"file_token": ft})
                total_kb += img_size // 1024

        if not file_tokens:
            print(f"  ❌ 记录 {success_count+1}: 所有图片上传失败，跳过")
            continue

        # 组装 Notes：主图来源信息
        if primary_src == "museum":
            source_name = primary_item.get("source_name", "Museum")
            notes = " | ".join(filter(None, [
                primary_item.get("title"), primary_item.get("artist"), primary_item.get("medium")
            ]))
        elif primary_src == "unsplash":
            source_name = "Unsplash"
            photographer = primary_item.get("photographer", "")
            notes = f"摄影师：{photographer}" if photographer else ""
        else:
            source_name = "Pexels"
            notes = ""

        record = {
            "fields": {
                "Asset Name｜素材名称": asset_name,
                "Date｜收集日期": now_ts,
                "Source｜来源平台": source_name,
                "Source Link｜来源链接": {"link": source_url, "text": source_url},
                "Notes｜备注": notes,
                **judge_fields,
                "Image / File｜图片或文件": file_tokens,
            }
        }
        records.append(record)
        src_icon = {"museum": "🏛", "unsplash": "🌿", "pexels": "🖼"}.get(primary_src, "📷")
        print(
            f"  {src_icon} 记录 {success_count+1}: {asset_name} "
            f"({len(file_tokens)}张/{total_kb}KB) "
            f"[{category} ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}]"
        )
        success_count += 1

    # 写入飞书
    if dry_run:
        print(f"\n🧪 Dry run 完成，预览 {success_count} 条候选记录（每条 {images_per_record} 张图），未写入飞书")
        return success_count

    if records:
        result = write_records_to_feishu(token, records, feishu_creds)
        if result.get("code") == 0:
            created = result.get("data", {}).get("records", [])
            print(f"\n🎉 成功写入 {len(created)} 条记录!")
            total_imgs = sum(
                len(r.get("fields", {}).get("Image / File｜图片或文件") or [])
                for r in created
            )
            print(f"   共 {total_imgs} 张图片附件（平均每条 {total_imgs//max(len(created),1)} 张）")
            print(f"   所有记录分类: {category}")
            return len(created)
        else:
            print(f"\n❌ 写入失败: {result.get('msg')}")
            return 0
    else:
        print("\n📋 无新记录（全部重复或搜索为空）")
        return 0


def run_music_collection(keyword, count, dry_run, token, feishu_creds, keywords_cfg, judge_cfg, music_cfg):
    """Tape 流程：搜音乐 → 上传封面 → 写入飞书。"""
    tracks = search_music(keyword, count, music_cfg)
    print(f"🎧 {music_cfg['provider']} 找到 {len(tracks)} 首")

    existing_links, existing_count = get_existing_links(token, feishu_creds)
    print(f"📋 飞书已有 {existing_count} 条, {len(existing_links)} 个唯一链接")

    if dry_run:
        print("🧪 Dry run: 只预览音乐评分，不下载封面、不写入飞书")

    now_ts = int(time.time())
    today_str = time.strftime("%Y-%m-%d")
    records = []
    success_count = 0

    for i, track in enumerate(tracks):
        if success_count >= count:
            break
        source_url = music_source_link(track)
        if source_url in existing_links:
            print(f"  ⏭ Track {i}: 已存在，跳过")
            continue

        judge_fields = get_music_judge_fields(
            keyword,
            keywords_cfg,
            judge_cfg,
            track,
            category="Tape",
        )
        judge_fields["Status｜状态"] = judge_cfg.get("default_status", "Raw｜未判断")
        judge_fields["Title｜标题"] = ""
        judge_fields["Content Angle｜内容角度"] = ""
        asset_name = build_music_asset_name(track)

        if dry_run:
            print(
                f"  🧪 Track {i}: {asset_name} "
                f"[Tape ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}]"
            )
            print(f"     {judge_fields['YONO Reason｜为什么适合 YONO']}")
            if track.get("previewUrl"):
                print(f"     将下载试听音频：{track['previewUrl']}")
            success_count += 1
            continue

        file_tokens = []
        audio_token = None
        preview_url = track.get("previewUrl")
        if preview_url:
            audio_filename = f"music_{today_str}_{i}_{safe_filename(asset_name)}.m4a"
            audio_path, audio_size = download_file(
                preview_url,
                audio_filename,
                headers={"User-Agent": "iTunes/12.0", "Range": "bytes=0-"},
                min_size=1024,
                expected_content_prefix="audio/",
            )
            if audio_path:
                audio_token = upload_to_feishu_drive(
                    token,
                    audio_path,
                    audio_size,
                    audio_filename,
                    feishu_creds,
                    mime_type="audio/mp4",
                )
                os.remove(audio_path)
                if audio_token:
                    file_tokens.append({"file_token": audio_token})
        if not audio_token:
            print(f"  ⏭ Track {i}: 音频无法下载或上传，跳过")
            continue

        artwork_url = music_artwork_url(track)
        if artwork_url:
            img_path, img_size = download_image(artwork_url, f"music_{i}")
            if img_path:
                filename = f"music_{today_str}_{i}.jpg"
                file_token = upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds)
                os.remove(img_path)
                if file_token:
                    file_tokens.append({"file_token": file_token})

        notes = []
        if track.get("previewUrl"):
            notes.append(f"试听链接：{track['previewUrl']}")
            if audio_token:
                notes.append("试听音频：已下载到附件")
        if track.get("collectionName"):
            notes.append(f"专辑：{track['collectionName']}")
        if track.get("primaryGenreName"):
            notes.append(f"类型：{track['primaryGenreName']}")

        record = {
            "fields": {
                "Asset Name｜素材名称": asset_name,
                "Date｜收集日期": now_ts,
                "Source｜来源平台": "Apple Music / iTunes",
                "Source Link｜来源链接": {"link": source_url, "text": source_url},
                "Notes｜备注": "；".join(notes),
                **judge_fields,
            }
        }
        if file_tokens:
            record["fields"]["Image / File｜图片或文件"] = file_tokens

        records.append(record)
        has_audio = "🎵" if audio_token else "❌无音频"
        print(
            f"  {has_audio} Track {i}: {asset_name} "
            f"[Tape ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}]"
        )
        success_count += 1

    if dry_run:
        print(f"\n🧪 Dry run 完成，预览 {success_count} 条音乐候选记录，未写入飞书")
        return success_count

    if records:
        result = write_records_to_feishu(token, records, feishu_creds)
        if result.get("code") == 0:
            created = result.get("data", {}).get("records", [])
            file_count = sum(1 for r in created if r.get("fields", {}).get("Image / File｜图片或文件"))
            print(f"\n🎉 成功写入 {len(created)} 条音乐记录!")
            print(f"   其中 {file_count} 条含音频/封面附件")
            return len(created)
        print(f"\n❌ 写入失败: {result.get('msg')}")
        return 0

    print("\n📋 无新音乐记录（全部重复或搜索为空）")
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

    # ── 补全发布内容：Status=Save 但缺 发布内容 的记录 ──
    post_updates = []
    for item in items:
        fields = item.get("fields", {})
        if fields.get("Status｜状态") != "Save｜保存":
            continue
        if fields.get("Post Content｜发布内容"):
            continue

        category = fields.get("Category｜分类", "")
        name = fields.get("Asset Name｜素材名称", "")
        title = fields.get("Title｜标题", "") or ""
        reason = fields.get("YONO Reason｜为什么适合 YONO", "") or ""
        source = fields.get("Source｜来源平台", "") or ""
        notes = fields.get("Notes｜备注", "") or ""
        alt = notes or name

        # 用已有的视觉/情绪标签反推信号词
        vtags = fields.get("Visual Tags｜视觉标签") or []
        vtags = vtags if isinstance(vtags, list) else [vtags]
        positive_hits = [t.replace("warm light", "warm").replace("soft color", "soft") for t in vtags]
        curiosity_point = build_curiosity_point(category, alt, [], [])

        if source in ("Apple Music / iTunes",) or category == "Tape":
            # 音乐类：从 Notes 里恢复 track 信息
            track = {}
            for line in str(notes).split("；"):
                if line.startswith("专辑："):
                    track["collectionName"] = line[3:]
                elif line.startswith("类型："):
                    track["primaryGenreName"] = line[3:]
            # 从 Asset Name 拆出 track - artist
            parts = name.split(" - ", 1)
            track["trackName"] = parts[0].strip()
            track["artistName"] = parts[1].strip() if len(parts) > 1 else ""
            post_content = build_xiaohongshu_music_post(track, title, curiosity_point, positive_hits, reason)
        else:
            post_content = build_xiaohongshu_post(
                category, title, alt, curiosity_point, positive_hits, [], reason
            )

        if post_content:
            post_updates.append({
                "record_id": item["record_id"],
                "fields": {"Post Content｜发布内容": post_content}
            })
            print(f"  ✍️  {name[:40]} → 发布内容已生成")

    if post_updates:
        total_post = 0
        for batch_start in range(0, len(post_updates), 5):
            batch = post_updates[batch_start:batch_start+5]
            r = requests.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"records": batch}
            ).json()
            if r.get("code") == 0:
                total_post += len(batch)
            else:
                print(f"  ❌ 发布内容写入失败: {r.get('msg')}")
        if total_post:
            print(f"\n✍️  补全发布内容 {total_post} 条！")
    else:
        print("无需补全发布内容（无 Save 素材或已全部补全）")


def fix_pexels_tape_records(dry_run=False):
    """修正 Pexels 图片被错误标为 Tape 的记录，按图片标准重新分类。"""
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)
    modules = judge_cfg.get("modules", {})

    token = get_feishu_token(feishu_creds)
    print(f"✅ Token 获取成功")

    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records",
        params={"page_size": 500},
        headers={"Authorization": f"Bearer {token}"}
    )
    items = resp.json().get("data", {}).get("items", [])
    print(f"📋 总共 {len(items)} 条记录，扫描 Pexels+Tape 误标...")

    updates = []
    for item in items:
        fields = item.get("fields", {})
        category = fields.get("Category｜分类", "")
        source = fields.get("Source｜来源平台", "")
        link_field = fields.get("Source Link｜来源链接", "")
        link = link_field.get("link", "") if isinstance(link_field, dict) else str(link_field)

        is_pexels = "pexels" in link.lower() or "pexels" in str(source).lower()
        if not (is_pexels and category == "Tape"):
            continue

        name = fields.get("Asset Name｜素材名称", "")
        new_category = classify_category(name + " " + link, keywords_cfg)
        # 如果还是推断成 Tape（音乐词），改为 Block 兜底
        if new_category == "Tape":
            new_category = "Block"

        judge = modules.get(new_category, modules.get("Block", {}))
        curiosity_point = build_curiosity_point(new_category, name, [], [])
        new_reason = (
            f"筛选理由：此条素材为 Pexels 图片，已从 Tape 重新归类为 {new_category}。"
            f" {judge.get('yono_reason', '')} "
            f"好奇心点：{curiosity_point}"
        )

        update_fields = {
            "Category｜分类": new_category,
            "YONO Reason｜为什么适合 YONO": new_reason,
            "Visual Tags｜视觉标签": judge.get("visual_tags", []),
            "Mood Tags｜情绪标签": judge.get("mood_tags", []),
        }
        if dry_run:
            print(f"  🧪 {item['record_id']} | {name[:40]} | Tape → {new_category}")
        else:
            updates.append({"record_id": item["record_id"], "fields": update_fields})
            print(f"  🔧 {item['record_id']} | {name[:40]} | Tape → {new_category}")

    if dry_run:
        print(f"\n🧪 Dry run 完成，共 {len([i for i in items if 'pexels' in str(i.get('fields',{}).get('Source Link｜来源链接','')).lower() and i.get('fields',{}).get('Category｜分类')=='Tape'])} 条需修正")
        return

    if not updates:
        print("未发现 Pexels+Tape 误标记录，无需修正")
        return

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
    print(f"\n✅ 修正 {total_updated} 条误标记录!")


def delete_rejected_records():
    """删除飞书表格中所有状态为 Reject｜放弃 的记录。"""
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)
    token = get_feishu_token(feishu_creds)

    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records",
        params={"page_size": 500},
        headers={"Authorization": f"Bearer {token}"}
    )
    items = resp.json().get("data", {}).get("items", [])
    to_delete = [
        item["record_id"] for item in items
        if item.get("fields", {}).get("Status｜状态") == "Reject｜放弃"
    ]
    if not to_delete:
        print("无放弃记录，无需删除")
        return 0

    print(f"🗑  发现 {len(to_delete)} 条放弃记录，开始删除...")
    deleted = 0
    for i in range(0, len(to_delete), 500):
        batch = to_delete[i:i+500]
        r = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_delete",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": batch}
        ).json()
        if r.get("code") == 0:
            deleted += len(batch)
            print(f"  ✅ 删除 {len(batch)} 条")
        else:
            print(f"  ❌ 删除失败: {r.get('msg')}")
    print(f"🗑  共删除 {deleted} 条放弃记录")
    return deleted


def list_records():
    """查看飞书表格现有记录"""
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)

    token = get_feishu_token(feishu_creds)
    links, count = get_existing_links(token, feishu_creds)
    print(f"飞书表格现有 {count} 条记录")
    return count


def fill_links(dry_run=False):
    """扫描飞书表格中只有 Source Link、缺少图片或评判字段的记录，自动补全。
    用法：在飞书新建一行，只填 Source Link，运行此命令即可补全所有字段。
    支持来源：Pinterest / Unsplash / Pexels / 任意图片直链 / 博物馆页面
    """
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)
    pexels_cfg = get_pexels_config(sources_cfg)

    token = get_feishu_token(feishu_creds)
    print(f"✅ Token 获取成功")

    resp = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records",
        params={"page_size": 500},
        headers={"Authorization": f"Bearer {token}"}
    )
    items = resp.json().get("data", {}).get("items", [])
    print(f"📋 总共 {len(items)} 条记录，扫描待补全...")

    today_str = time.strftime("%Y-%m-%d")
    now_ts = int(time.time())
    pending = []

    for item in items:
        fields = item.get("fields", {})
        link_field = fields.get("Source Link｜来源链接", "")
        link = link_field.get("link", "") if isinstance(link_field, dict) else str(link_field or "")
        if not link:
            continue
        # 判断是否需要补全：缺图片 或 缺评判字段
        has_image = bool(fields.get("Image / File｜图片或文件"))
        has_score = bool(fields.get("Score｜评分"))
        if has_image and has_score:
            continue
        pending.append((item["record_id"], fields, link))

    print(f"🔍 发现 {len(pending)} 条待补全记录")
    if not pending:
        return

    updates = []
    for record_id, fields, link in pending:
        print(f"\n  处理: {link[:70]}")

        # 从链接或已有字段推断分类
        existing_cat = fields.get("Category｜分类", "")
        if existing_cat and existing_cat not in ("Block", "OOTD"):
            category = existing_cat
        else:
            category = classify_category(link, keywords_cfg)
            if category in ("Block", "OOTD"):
                category = "Archive"

        # 尝试提取可直接下载的图片 URL
        img_url = _extract_image_url(link)
        if not img_url:
            print(f"    ⚠️ 无法从链接提取图片，跳过（可手动上传图片）")
            continue

        # 下载图片
        img_path, img_size = download_image(img_url, f"fill_{record_id[:8]}")
        if not img_path:
            print(f"    ❌ 图片下载失败")
            continue

        # 用图片 alt / 链接文字作为描述交给评判引擎
        alt = fields.get("Notes｜备注", "") or link
        pseudo_photo = {"alt": alt, "width": 1200, "height": 1200}
        judge_fields = get_judge_fields(link, keywords_cfg, judge_cfg, category=category, photo=pseudo_photo)

        asset_name = fields.get("Asset Name｜素材名称", "") or build_asset_name(
            category,
            alt=alt,
            visual_tags=judge_fields.get("Visual Tags｜视觉标签", []),
            reason=judge_fields.get("YONO Reason｜为什么适合 YONO", ""),
        )

        source_name = _guess_source_name(link)

        if dry_run:
            os.remove(img_path)
            print(f"    🧪 {asset_name} [{category} ★{judge_fields['Score｜评分']}] 来源: {source_name}")
            continue

        # 上传图片
        filename = f"fill_{today_str}_{record_id[:8]}.jpg"
        file_token = upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds)
        os.remove(img_path)

        update_fields = {
            "Asset Name｜素材名称": asset_name,
            "Date｜收集日期": fields.get("Date｜收集日期") or now_ts,
            "Source｜来源平台": fields.get("Source｜来源平台") or source_name,
            **judge_fields,
        }
        if file_token:
            update_fields["Image / File｜图片或文件"] = [{"file_token": file_token}]

        updates.append({"record_id": record_id, "fields": update_fields})
        print(f"    ✅ {asset_name} [{category} ★{judge_fields['Score｜评分']} {judge_fields['Status｜状态']}] {img_size//1024}KB")

    if dry_run:
        print(f"\n🧪 Dry run 完成，共 {len(pending)} 条待处理")
        return

    if not updates:
        print("\n无记录可更新")
        return

    # 批量写回飞书
    total = 0
    for i in range(0, len(updates), 5):
        batch = updates[i:i+5]
        r = requests.post(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": batch}
        ).json()
        if r.get("code") == 0:
            total += len(batch)
        else:
            print(f"  ❌ 批次失败: {r.get('msg')}")
    print(f"\n🎉 补全 {total} 条记录！")


def _extract_image_url(link):
    """从页面链接提取可直接下载的图片 URL。"""
    # 已经是图片直链
    if any(link.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
        return link
    # Pinterest 图片直链格式
    if "pinimg.com" in link:
        return link
    # Unsplash 图片页面 → 拼 download URL
    if "unsplash.com/photos/" in link:
        photo_id = link.rstrip("/").split("/")[-1].split("?")[0]
        return f"https://source.unsplash.com/{photo_id}/1200x1200"
    # Pexels 图片页面 → 尝试抓 og:image
    if "pexels.com" in link or "unsplash.com" in link or "pinterest" in link:
        return _fetch_og_image(link)
    # 通用：尝试抓 og:image
    return _fetch_og_image(link)


def _fetch_og_image(url):
    """从页面 HTML 提取 og:image。"""
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        html = resp.text
        # og:image
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']',
            r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            import re
            m = re.search(pattern, html)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _guess_source_name(link):
    """从 URL 猜测来源平台名称。"""
    if "pinterest" in link or "pinimg" in link:
        return "Pinterest"
    if "unsplash" in link:
        return "Unsplash"
    if "pexels" in link:
        return "Pexels"
    if "metmuseum" in link:
        return "The Metropolitan Museum of Art"
    if "artic.edu" in link:
        return "Art Institute of Chicago"
    if "rijksmuseum" in link:
        return "Rijksmuseum"
    try:
        from urllib.parse import urlparse
        return urlparse(link).netloc.replace("www.", "")
    except Exception:
        return "Web"


def _extract_field_notes_keyword(title, post_content):
    """从 Field Notes 的标题和发布内容里提取适合搜图的英文关键词。"""
    import re
    # 取标题前20字 + post_content前50字，去掉中文标点
    text = f"{title} {post_content}"[:120]
    # 过滤掉中文字符，保留英文单词和数字
    en_words = re.findall(r"[a-zA-Z]{3,}", text)
    stopwords = {"the", "and", "for", "that", "with", "this", "are", "you", "can",
                 "have", "will", "from", "not", "but", "its", "into", "was", "been"}
    en_words = [w.lower() for w in en_words if w.lower() not in stopwords]
    # 如果英文词太少，加上通用 Field Notes 搜图词
    if len(en_words) < 2:
        en_words = ["notebook", "desk", "inspiration", "observation"]
    return " ".join(en_words[:5])


def watch_and_fill(interval=180):
    """每隔 interval 秒检查飞书表格：
    - 普通分类：Status=Save → 补全 Title / Post Content
    - Field Notes：Status=Save + 有 Title + 有 Post Content + 无图片 → 自动根据内容搜图写入
    Ctrl+C 退出。
    """
    keywords_cfg, judge_cfg, sources_cfg = load_configs()
    feishu_creds = get_feishu_credentials(sources_cfg)
    pexels_cfg = get_pexels_config(sources_cfg)
    unsplash_cfg = get_unsplash_config(sources_cfg)

    print(f"👁  Watch 模式启动，每 {interval//60} 分钟检查一次。Ctrl+C 退出。")

    while True:
        try:
            token = get_feishu_token(feishu_creds)
            resp = requests.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records",
                params={"page_size": 500},
                headers={"Authorization": f"Bearer {token}"}
            )
            items = resp.json().get("data", {}).get("items", [])
            now_str = time.strftime("%H:%M:%S")
            today_str = time.strftime("%Y-%m-%d")
            now_ts = int(time.time())

            # ── Field Notes 逆向流程：用户先写内容，系统去找图 ──
            fn_pending = [
                item for item in items
                if item.get("fields", {}).get("Category｜分类") == "Field Notes"
                and item.get("fields", {}).get("Status｜状态") == "Save｜保存"
                and item.get("fields", {}).get("Title｜标题")
                and item.get("fields", {}).get("Post Content｜发布内容")
                and not item.get("fields", {}).get("Image / File｜图片或文件")
            ]
            if fn_pending:
                print(f"[{now_str}] 📓 Field Notes: {len(fn_pending)} 条需要自动配图")
            for item in fn_pending:
                fields = item.get("fields", {})
                title = str(fields.get("Title｜标题") or "")
                post_content = str(fields.get("Post Content｜发布内容") or "")
                name = fields.get("Asset Name｜素材名称", "") or title[:20]
                keyword = _extract_field_notes_keyword(title, post_content)
                print(f"  🔍 搜图关键词: {keyword}  ← {name[:30]}")

                # 优先 Unsplash，没有 Key 用 Pexels，搜 5 张候选
                photos = []
                if unsplash_cfg.get("api_key"):
                    photos = search_unsplash(keyword, 10, unsplash_cfg)
                if not photos:
                    photos = search_pexels(keyword, 10, pexels_cfg)

                if not photos:
                    print(f"  ⚠️ 搜图失败，跳过")
                    continue

                source_name = "Unsplash" if unsplash_cfg.get("api_key") and photos else "Pexels"
                source_url = photos[0].get("url", "") if photos else ""

                # 下载并上传 3-5 张图
                file_tokens = []
                for pi, p in enumerate(photos[:5]):
                    img_url = p["src"].get("large") or p["src"].get("original", "")
                    img_path, img_size = download_image(img_url, f"fn_{item['record_id'][:8]}_{pi}")
                    if not img_path:
                        continue
                    filename = f"fn_{today_str}_{item['record_id'][:8]}_{pi}.jpg"
                    ft = upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds)
                    os.remove(img_path)
                    if ft:
                        file_tokens.append({"file_token": ft})
                    if len(file_tokens) >= 4:
                        break

                if not file_tokens:
                    print(f"  ⚠️ 图片上传失败，跳过")
                    continue

                # 补全评判字段（用 title+post_content 作为 alt）
                alt = f"{title} {post_content[:80]}"
                pseudo_photo = {"alt": alt, "width": 1200, "height": 1200}
                judge_fields = get_judge_fields(keyword, keywords_cfg, judge_cfg, category="Field Notes", photo=pseudo_photo)

                update_fields = {
                    "Source｜来源平台": source_name,
                    "Source Link｜来源链接": {"link": source_url, "text": source_url},
                    "Date｜收集日期": fields.get("Date｜收集日期") or now_ts,
                    "Score｜评分": judge_fields["Score｜评分"],
                    "YONO Reason｜为什么适合 YONO": judge_fields["YONO Reason｜为什么适合 YONO"],
                    "Content Angle｜内容角度": judge_fields["Content Angle｜内容角度"],
                    "Visual Tags｜视觉标签": judge_fields["Visual Tags｜视觉标签"],
                    "Mood Tags｜情绪标签": judge_fields["Mood Tags｜情绪标签"],
                    "Image / File｜图片或文件": file_tokens,
                }

                r = requests.post(
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"records": [{"record_id": item["record_id"], "fields": update_fields}]}
                ).json()
                if r.get("code") == 0:
                    print(f"  🖼  {name[:40]} → 配图完成 ({len(file_tokens)}张) [{source_name}]")
                else:
                    print(f"  ❌ 写入失败: {r.get('msg')}")

            # ── 扫描只有 Source Link 的空白记录，自动补全图片 + 评判字段 ──
            link_pending = [
                item for item in items
                if (
                    (item.get("fields", {}).get("Source Link｜来源链接") or "")
                    and not item.get("fields", {}).get("Image / File｜图片或文件")
                    and not item.get("fields", {}).get("Score｜评分")
                )
            ]
            if link_pending:
                print(f"[{now_str}] 🔗 发现 {len(link_pending)} 条只有来源链接待补全")
            for item in link_pending:
                fields = item.get("fields", {})
                link_field = fields.get("Source Link｜来源链接", "")
                link = link_field.get("link", "") if isinstance(link_field, dict) else str(link_field or "")
                if not link:
                    continue
                print(f"  处理: {link[:70]}")

                existing_cat = fields.get("Category｜分类", "")
                category = existing_cat if existing_cat and existing_cat not in ("Block", "OOTD") else "Archive"

                img_url = _extract_image_url(link)
                if not img_url:
                    print(f"    ⚠️ 无法提取图片 URL，跳过（可手动上传）")
                    continue

                img_path, img_size = download_image(img_url, f"watch_fill_{item['record_id'][:8]}")
                if not img_path:
                    print(f"    ❌ 图片下载失败")
                    continue

                alt = fields.get("Notes｜备注", "") or link
                pseudo_photo = {"alt": alt, "width": 1200, "height": 1200}
                judge_fields = get_judge_fields(link, keywords_cfg, judge_cfg, category=category, photo=pseudo_photo)

                asset_name = fields.get("Asset Name｜素材名称", "") or build_asset_name(
                    category,
                    alt=alt,
                    visual_tags=judge_fields.get("Visual Tags｜视觉标签", []),
                    reason=judge_fields.get("YONO Reason｜为什么适合 YONO", ""),
                )
                source_name = _guess_source_name(link)
                filename = f"watch_{today_str}_{item['record_id'][:8]}.jpg"
                file_token = upload_to_feishu_drive(token, img_path, img_size, filename, feishu_creds)
                os.remove(img_path)

                update_fields = {
                    "Asset Name｜素材名称": asset_name,
                    "Date｜收集日期": fields.get("Date｜收集日期") or now_ts,
                    "Source｜来源平台": fields.get("Source｜来源平台") or source_name,
                    **judge_fields,
                }
                if file_token:
                    update_fields["Image / File｜图片或文件"] = [{"file_token": file_token}]

                r = requests.post(
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"records": [{"record_id": item["record_id"], "fields": update_fields}]}
                ).json()
                if r.get("code") == 0:
                    print(f"    ✅ {asset_name} [{category} ★{judge_fields['Score｜评分']}] {img_size//1024}KB [{source_name}]")
                else:
                    print(f"    ❌ 写入失败: {r.get('msg')}")

            # ── 普通分类：Status=Save → 补全 Title / Post Content ──
            pending = [
                item for item in items
                if item.get("fields", {}).get("Status｜状态") == "Save｜保存"
                and item.get("fields", {}).get("Category｜分类") != "Field Notes"
                and (
                    not item.get("fields", {}).get("Title｜标题")
                    or not item.get("fields", {}).get("Post Content｜发布内容")
                )
            ]

            if not fn_pending and not pending:
                print(f"[{now_str}] ✅ 无待补全记录")
            elif pending:
                print(f"[{now_str}] 🔍 发现 {len(pending)} 条 Save 记录待补全 Title/Post Content")
                updates = []
                for item in pending:
                    fields = item.get("fields", {})
                    category = fields.get("Category｜分类", "")
                    name = fields.get("Asset Name｜素材名称", "")
                    reason = fields.get("YONO Reason｜为什么适合 YONO", "") or ""
                    notes = fields.get("Notes｜备注", "") or ""
                    alt = notes or name
                    source = fields.get("Source｜来源平台", "") or ""
                    vtags = fields.get("Visual Tags｜视觉标签") or []
                    vtags = vtags if isinstance(vtags, list) else [vtags]
                    positive_hits = [t for t in vtags]
                    curiosity_point = build_curiosity_point(category, alt, [], [])

                    is_music = source in ("Apple Music / iTunes",) or category == "Tape"
                    if is_music:
                        track = {}
                        for line in str(notes).split("；"):
                            if line.startswith("专辑："):
                                track["collectionName"] = line[3:]
                            elif line.startswith("类型："):
                                track["primaryGenreName"] = line[3:]
                        parts = name.split(" - ", 1)
                        track["trackName"] = parts[0].strip()
                        track["artistName"] = parts[1].strip() if len(parts) > 1 else ""
                        title = fields.get("Title｜标题") or build_music_title(track, positive_hits)
                        post_content = build_xiaohongshu_music_post(track, title, curiosity_point, positive_hits, reason)
                    else:
                        title = fields.get("Title｜标题") or build_xiaohongshu_title(category, alt, positive_hits, [])
                        post_content = build_xiaohongshu_post(category, title, alt, curiosity_point, positive_hits, [], reason)

                    update_fields = {}
                    if not fields.get("Title｜标题"):
                        update_fields["Title｜标题"] = title
                    if not fields.get("Post Content｜发布内容") and post_content:
                        update_fields["Post Content｜发布内容"] = post_content

                    if update_fields:
                        updates.append({"record_id": item["record_id"], "fields": update_fields})
                        print(f"  ✍️  {name[:40]} → {list(update_fields.keys())}")

                for i in range(0, len(updates), 5):
                    batch = updates[i:i+5]
                    r = requests.post(
                        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json={"records": batch}
                    ).json()
                    if r.get("code") != 0:
                        print(f"  ❌ 写入失败: {r.get('msg')}")
                if updates:
                    print(f"  ✅ 补全 {len(updates)} 条")

            # ── 补全空素材名称 ──
            name_updates = []
            for item in items:
                fields = item.get("fields", {})
                if fields.get("Asset Name｜素材名称"):
                    continue
                category = fields.get("Category｜分类", "")
                vtags = fields.get("Visual Tags｜视觉标签") or []
                reason = fields.get("YONO Reason｜为什么适合 YONO", "") or ""
                notes = fields.get("Notes｜备注", "") or ""
                link_field = fields.get("Source Link｜来源链接", "")
                link_text = link_field.get("link", "") if isinstance(link_field, dict) else str(link_field or "")
                alt = notes or link_text.rstrip("/").split("/")[-1].replace("-", " ")
                generated = build_asset_name(category, alt=alt, visual_tags=vtags, reason=reason)
                if generated:
                    name_updates.append({"record_id": item["record_id"], "fields": {"Asset Name｜素材名称": generated}})
                    print(f"  🏷  空素材名 → {generated}  [{category}]")

            if name_updates:
                for i in range(0, len(name_updates), 5):
                    batch = name_updates[i:i+5]
                    r = requests.post(
                        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{feishu_creds['app_token']}/tables/{feishu_creds['table_id']}/records/batch_update",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json={"records": batch}
                    ).json()
                    if r.get("code") != 0:
                        print(f"  ❌ 素材名写入失败: {r.get('msg')}")
                print(f"  ✅ 补全素材名 {len(name_updates)} 条")

        except KeyboardInterrupt:
            print("\n👁  Watch 模式已退出。")
            break
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️ 检查出错: {e}")

        time.sleep(interval)


def collect_all(count_per_category=5, images_per_record=4, dry_run=False):
    """按分类顺序批量抓取：Archive → Postcard → Field Notes → Tape，每个分类各 count 条。
    写入飞书后记录天然按分类成块排列。每条记录附 images_per_record 张图（默认4张）。
    """
    category_keywords = {
        "Archive":     "vintage brand design photography",
        "Postcard":    "warm quiet morning light emotion",
        "Field Notes": "notebook desk inspiration journal",
        "Tape":        "ambient indie soundtrack",
    }
    order = ["Archive", "Postcard", "Field Notes", "Tape"]

    total = 0
    for cat in order:
        keyword = category_keywords[cat]
        print(f"\n{'='*50}")
        print(f"▶ 分类: {cat}  关键词: {keyword}  目标: {count_per_category} 条 x {images_per_record} 张/条")
        print(f"{'='*50}")
        n = run(keyword=keyword, count=count_per_category, images_per_record=images_per_record, dry_run=dry_run)
        total += (n or 0)

    print(f"\n🎉 collect-all 完成，共写入 {total} 条记录（{len(order)} 个分类各 {count_per_category} 条，每条 {images_per_record} 张图）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YONO Collector: Pexels → 飞书（OS.md 标准）")
    parser.add_argument("--keyword", "-k", help="搜索关键词（默认按星期轮换）")
    parser.add_argument("--count", "-n", type=int, default=5, help="每次抓取记录数量（默认5）")
    parser.add_argument("--images", "-i", type=int, default=4, help="每条图片记录附几张图（3-5，默认4）")
    parser.add_argument("--list", "-l", action="store_true", help="只查看飞书记录数")
    parser.add_argument("--backfill", "-b", action="store_true", help="回填已有记录的空评判字段")
    parser.add_argument("--fix-tape", action="store_true", help="修正 Pexels 图片被错误标为 Tape 的记录")
    parser.add_argument("--fill-links", action="store_true", help="补全飞书中只有 Source Link 的空白记录")
    parser.add_argument("--watch", "-w", action="store_true", help="监听模式：每3分钟自动补全 Save 记录的 Title/Post Content")
    parser.add_argument("--collect-all", action="store_true", help="按分类顺序批量抓取 Archive→Postcard→Field Notes→Tape，每类各5条")
    parser.add_argument("--delete-rejected", action="store_true", help="删除飞书表格中所有状态为 Reject｜放弃 的记录")
    parser.add_argument("--dry-run", action="store_true", help="只预览评分，不下载、不上传、不写入飞书")
    args = parser.parse_args()

    if args.delete_rejected:
        delete_rejected_records()
    elif args.list:
        list_records()
    elif args.backfill:
        backfill_records()
    elif args.fix_tape:
        fix_pexels_tape_records(dry_run=args.dry_run)
    elif args.fill_links:
        fill_links(dry_run=args.dry_run)
    elif args.watch:
        watch_and_fill(interval=180)
    elif args.collect_all:
        collect_all(count_per_category=args.count, images_per_record=args.images, dry_run=args.dry_run)
    else:
        run(keyword=args.keyword, count=args.count, images_per_record=args.images, dry_run=args.dry_run)
