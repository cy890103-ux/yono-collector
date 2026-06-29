#!/usr/bin/env python3
"""Fill title and content angle for records whose status is Save｜保存."""

import os
import sys

import requests
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
API = "https://open.feishu.cn/open-apis"
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


def batch_update(token, creds, records):
    for start in range(0, len(records), 500):
        batch = records[start:start + 500]
        request_json(
            "POST",
            f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/records/batch_update",
            token=token,
            json={"records": batch},
        )
        print(f"已更新 {len(batch)} 条")


def main():
    dry_run = "--dry-run" in sys.argv
    keywords_cfg = load_yaml(os.path.join(CONFIG_DIR, "keywords.yaml"))
    creds = get_creds()
    token = get_token(creds)
    records = list_records(token, creds)

    updates = []
    save_count = 0
    clear_count = 0
    for record in records:
        fields = record.get("fields", {})
        status = fields.get("Status｜状态", "")
        if status != "Save｜保存":
            if fields.get("Title｜标题") or fields.get("Content Angle｜内容角度"):
                updates.append({
                    "record_id": record["record_id"],
                    "fields": {
                        "Title｜标题": "",
                        "Content Angle｜内容角度": "",
                    },
                })
                clear_count += 1
                print(f"{record['record_id']} | {status or '空状态'} | 清空标题/内容角度")
            continue

        name = fields.get("Asset Name｜素材名称", "")
        reason = fields.get("YONO Reason｜为什么适合 YONO", "")
        category = fields.get("Category｜分类") or classify_category(name, keywords_cfg)
        visual_tags = fields.get("Visual Tags｜视觉标签") or []

        if isinstance(visual_tags, str):
            signals = [visual_tags]
        elif isinstance(visual_tags, list):
            signals = [str(item) for item in visual_tags]
        else:
            signals = []

        alt = extract_alt_from_name(name)
        curiosity_point = build_curiosity_point(category, alt or name, signals, signals)
        title = build_xiaohongshu_title(category, alt or name, signals, signals)
        content_angle = build_xiaohongshu_content_angle(
            category,
            alt or name,
            reason,
            curiosity_point,
        )

        updates.append({
            "record_id": record["record_id"],
            "fields": {
                "Title｜标题": title,
                "Content Angle｜内容角度": content_angle,
            },
        })
        save_count += 1
        print(f"{record['record_id']} | {status} | {title}")

    if dry_run:
        print(f"Dry run: 将生成 {save_count} 条 Save 文案，清空 {clear_count} 条非 Save 文案")
        return 0

    if updates:
        batch_update(token, creds, updates)
    print(f"完成：已生成 {save_count} 条 Save 文案，清空 {clear_count} 条非 Save 文案")
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
