#!/usr/bin/env python3
"""Apply YONO Base automation rules.

Rules:
- Status is Save｜保存: generate Title｜标题 and Content Angle｜内容角度.
- Status is Reject｜放弃: mark Rejected At｜放弃时间, then delete the row after 7 days.
- Other statuses: clear generated title/content and clear rejected timestamp.
"""

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys

import requests
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
API = "https://open.feishu.cn/open-apis"
PYTHON = "/Users/mac/.workbuddy/binaries/python/envs/default/bin/python3"

sys.path.insert(0, ROOT)

from yono_collector import (  # noqa: E402
    build_curiosity_point,
    build_xiaohongshu_content_angle,
    build_xiaohongshu_title,
    classify_category,
)


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def get_creds():
    sources = load_yaml(os.path.join(CONFIG_DIR, "sources.yaml"))
    return {
        "app_id": sources["feishu"]["app_id"],
        "app_secret": sources["feishu"]["app_secret"],
        "app_token": sources["feishu"]["app_token"],
        "table_id": sources["feishu"]["table_id"],
    }


def request_json(method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "json" in kwargs:
        headers["Content-Type"] = "application/json"

    resp = requests.request(
        method,
        f"{API}{path}",
        headers=headers,
        timeout=30,
        **kwargs,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"{method} {path} failed: {data}")
    return data.get("data", {})


def get_token(creds):
    resp = requests.post(
        f"{API}/auth/v3/tenant_access_token/internal",
        json={"app_id": creds["app_id"], "app_secret": creds["app_secret"]},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 Token 失败: {data}")
    return data["tenant_access_token"]


def list_fields(token, creds):
    data = request_json(
        "GET",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields",
        token=token,
        params={"page_size": 100},
    )
    return data.get("items", [])


def ensure_text_field(token, creds, field_name):
    fields = list_fields(token, creds)
    for field in fields:
        if field.get("field_name") == field_name:
            return field

    data = request_json(
        "POST",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields",
        token=token,
        json={
            "field_name": field_name,
            "type": 1,
        },
    )
    field = data.get("field", data)
    print(f"已创建字段: {field_name} ({field.get('field_id')})")
    return field


def list_records(token, creds):
    records = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = request_json(
            "GET",
            f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/records",
            token=token,
            params=params,
        )
        records.extend(data.get("items", []))
        if not data.get("has_more"):
            return records
        page_token = data.get("page_token")


def batch_update(token, creds, records, dry_run=False):
    if dry_run:
        return
    for start in range(0, len(records), 500):
        batch = records[start:start + 500]
        request_json(
            "POST",
            f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/records/batch_update",
            token=token,
            json={"records": batch},
        )
        print(f"已更新 {len(batch)} 条")


def delete_records(token, creds, record_ids, dry_run=False):
    for record_id in record_ids:
        if dry_run:
            print(f"Dry run: 将删除 {record_id}")
            continue
        request_json(
            "DELETE",
            f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/records/{record_id}",
            token=token,
        )
        print(f"已删除 {record_id}")


def generated_copy(fields, keywords_cfg):
    name = field_text(fields.get("Asset Name｜素材名称", ""))
    reason = field_text(fields.get("YONO Reason｜为什么适合 YONO", ""))
    category = field_text(fields.get("Category｜分类")) or classify_category(name, keywords_cfg)
    visual_tags = fields.get("Visual Tags｜视觉标签") or []

    if isinstance(visual_tags, str):
        signals = [visual_tags]
    elif isinstance(visual_tags, list):
        signals = [field_text(item) for item in visual_tags]
    else:
        signals = []

    alt = extract_alt_from_name(name) or name
    curiosity_point = build_curiosity_point(category, alt, signals, signals)
    return {
        "Title｜标题": build_xiaohongshu_title(category, alt, signals, signals),
        "Content Angle｜内容角度": build_xiaohongshu_content_angle(
            category,
            alt,
            reason,
            curiosity_point,
        ),
    }


def field_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or value.get("name") or "")
    return str(value)


def is_save_status(status):
    text = field_text(status)
    return "Save" in text or "保存" in text


def is_reject_status(status):
    text = field_text(status)
    return "Reject" in text or "放弃" in text


def today_text(now):
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def parse_date(value):
    text = field_text(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_alt_from_name(name):
    if not name:
        return ""
    parts = str(name).split("_Pexels_", 1)
    if len(parts) == 2:
        tail = parts[1]
        pieces = tail.split("_", 1)
        if len(pieces) == 2:
            return pieces[1].strip()
    return name


def apply_rules(dry_run=False, delete_after_days=7):
    keywords_cfg = load_yaml(os.path.join(CONFIG_DIR, "keywords.yaml"))
    creds = get_creds()
    token = get_token(creds)

    ensure_text_field(token, creds, "Title｜标题")
    ensure_text_field(token, creds, "Rejected At｜放弃时间")

    records = list_records(token, creds)
    now = datetime.now(timezone.utc)
    reject_cutoff = now - timedelta(days=delete_after_days)

    updates = []
    deletes = []
    save_count = 0
    reject_mark_count = 0
    clear_count = 0

    for record in records:
        record_id = record["record_id"]
        fields = record.get("fields", {})
        status = fields.get("Status｜状态", "")

        if is_save_status(status):
            copy_fields = generated_copy(fields, keywords_cfg)
            copy_fields["Rejected At｜放弃时间"] = ""
            updates.append({"record_id": record_id, "fields": copy_fields})
            save_count += 1
            print(f"{record_id} | Save | {copy_fields['Title｜标题']}")
            continue

        if is_reject_status(status):
            rejected_at = parse_date(fields.get("Rejected At｜放弃时间"))
            if rejected_at and rejected_at <= reject_cutoff:
                deletes.append(record_id)
                print(f"{record_id} | Reject 超过 {delete_after_days} 天 | 删除")
                continue

            if not rejected_at:
                updates.append({
                    "record_id": record_id,
                    "fields": {
                        "Title｜标题": "",
                        "Content Angle｜内容角度": "",
                        "Rejected At｜放弃时间": today_text(now),
                    },
                })
                reject_mark_count += 1
                print(f"{record_id} | Reject | 标记放弃时间 {today_text(now)}")
            elif fields.get("Title｜标题") or fields.get("Content Angle｜内容角度"):
                updates.append({
                    "record_id": record_id,
                    "fields": {
                        "Title｜标题": "",
                        "Content Angle｜内容角度": "",
                    },
                })
                clear_count += 1
                print(f"{record_id} | Reject | 清空标题/内容角度")
            continue

        if (
            fields.get("Title｜标题")
            or fields.get("Content Angle｜内容角度")
            or fields.get("Rejected At｜放弃时间")
        ):
            updates.append({
                "record_id": record_id,
                "fields": {
                    "Title｜标题": "",
                    "Content Angle｜内容角度": "",
                    "Rejected At｜放弃时间": "",
                },
            })
            clear_count += 1
            print(f"{record_id} | 非 Save/Reject | 清空自动化字段")

    if dry_run:
        print(
            f"Dry run: Save 生成 {save_count} 条，Reject 标记 {reject_mark_count} 条，"
            f"清空 {clear_count} 条，删除 {len(deletes)} 条"
        )
        return

    batch_update(token, creds, updates)
    delete_records(token, creds, deletes)
    print(
        f"完成：Save 生成 {save_count} 条，Reject 标记 {reject_mark_count} 条，"
        f"清空 {clear_count} 条，删除 {len(deletes)} 条"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delete-after-days", type=int, default=7)
    args = parser.parse_args()
    apply_rules(dry_run=args.dry_run, delete_after_days=args.delete_after_days)


if __name__ == "__main__":
    main()
