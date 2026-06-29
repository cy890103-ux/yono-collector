#!/usr/bin/env python3
"""Normalize asset names and use-platform field."""

import os
import sys

import requests
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
API = "https://open.feishu.cn/open-apis"
sys.path.insert(0, ROOT)

from yono_collector import build_asset_name  # noqa: E402


PLATFORM_FIELD = "Use Platform｜使用平台"
OLD_PLATFORM_FIELD = "Used In｜使用位置"
PLATFORM_OPTIONS = ["小红书", "抖音", "Instagram", "X"]


def load_sources():
    with open(os.path.join(CONFIG_DIR, "sources.yaml")) as f:
        return yaml.safe_load(f)


def get_creds():
    sources = load_sources()
    return {
        "app_id": sources["feishu"]["app_id"],
        "app_secret": sources["feishu"]["app_secret"],
        "app_token": sources["feishu"]["app_token"],
        "table_id": sources["feishu"]["table_id"],
    }


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


def request_json(method, path, token, **kwargs):
    headers = kwargs.pop("headers", {})
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


def list_fields(token, creds):
    data = request_json(
        "GET",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields",
        token,
        params={"page_size": 100},
    )
    return data.get("items", [])


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
            token,
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
            token,
            json={"records": batch},
        )
        print(f"已更新 {len(batch)} 条")


def update_field(token, creds, field_id, payload):
    return request_json(
        "PUT",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields/{field_id}",
        token,
        json=payload,
    )


def create_platform_field(token, creds):
    return request_json(
        "POST",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields",
        token,
        json=platform_payload(),
    )


def delete_field(token, creds, field_id):
    return request_json(
        "DELETE",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields/{field_id}",
        token,
    )


def platform_payload():
    return {
        "field_name": PLATFORM_FIELD,
        "type": 4,
        "property": {
            "options": [
                {"name": name, "color": index}
                for index, name in enumerate(PLATFORM_OPTIONS)
            ]
        },
    }


def ensure_platform_field(token, creds):
    fields = list_fields(token, creds)
    by_name = {field.get("field_name"): field for field in fields}

    if PLATFORM_FIELD in by_name:
        field = by_name[PLATFORM_FIELD]
        update_field(token, creds, field["field_id"], platform_payload())
        print(f"已确认平台字段: {PLATFORM_FIELD}")
        return

    old = by_name.get(OLD_PLATFORM_FIELD)
    if old:
        try:
            update_field(token, creds, old["field_id"], platform_payload())
            print(f"已改字段: {OLD_PLATFORM_FIELD} -> {PLATFORM_FIELD}")
            return
        except Exception as error:
            print(f"原字段无法直接改成多选，将创建新字段并删除旧字段: {error}")
            create_platform_field(token, creds)
            delete_field(token, creds, old["field_id"])
            print(f"已创建 {PLATFORM_FIELD} 并删除 {OLD_PLATFORM_FIELD}")
            return

    create_platform_field(token, creds)
    print(f"已创建平台字段: {PLATFORM_FIELD}")


def normalize_names(token, creds, dry_run=False):
    records = list_records(token, creds)
    updates = []
    for record in records:
        fields = record.get("fields", {})
        category = field_text(fields.get("Category｜分类")) or "Tape"
        visual_tags = fields.get("Visual Tags｜视觉标签") or []
        if isinstance(visual_tags, str):
            visual_tags = [visual_tags]
        elif not isinstance(visual_tags, list):
            visual_tags = []

        alt = field_text(fields.get("Title｜标题")) or field_text(fields.get("Asset Name｜素材名称"))
        reason = field_text(fields.get("YONO Reason｜为什么适合 YONO"))
        name = build_asset_name(category, alt=alt, visual_tags=visual_tags, reason=reason)

        updates.append({
            "record_id": record["record_id"],
            "fields": {
                "Asset Name｜素材名称": name,
            },
        })
        print(f"{record['record_id']} | {category} | {name}")

    batch_update(token, creds, updates, dry_run=dry_run)
    print(f"{'Dry run: ' if dry_run else ''}素材名称处理 {len(updates)} 条")


def field_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("link") or "")
    return str(value)


def main():
    dry_run = "--dry-run" in sys.argv
    creds = get_creds()
    token = get_token(creds)
    ensure_platform_field(token, creds)
    normalize_names(token, creds, dry_run=dry_run)


if __name__ == "__main__":
    main()
