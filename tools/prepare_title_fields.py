#!/usr/bin/env python3
"""Prepare title/copy fields in Feishu Bitable.

- Ensure the text field "Title｜标题" exists.
- Clear all existing values in "Title｜标题" and "Content Angle｜内容角度".
"""

import os
import sys

import requests
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
API = "https://open.feishu.cn/open-apis"


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


def ensure_title_field(token, creds):
    fields = list_fields(token, creds)
    for field in fields:
        if field.get("field_name") == "Title｜标题":
            print(f"标题字段已存在: {field.get('field_id')}")
            return field

    data = request_json(
        "POST",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields",
        token=token,
        json={
            "field_name": "Title｜标题",
            "type": 1,
        },
    )
    field = data.get("field", data)
    print(f"已创建标题字段: {field.get('field_id')}")
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


def batch_update(token, creds, records):
    for start in range(0, len(records), 500):
        batch = records[start:start + 500]
        request_json(
            "POST",
            f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/records/batch_update",
            token=token,
            json={"records": batch},
        )
        print(f"已清空 {len(batch)} 条")


def main():
    creds = get_creds()
    token = get_token(creds)
    ensure_title_field(token, creds)

    records = list_records(token, creds)
    updates = [
        {
            "record_id": record["record_id"],
            "fields": {
                "Title｜标题": "",
                "Content Angle｜内容角度": "",
            },
        }
        for record in records
    ]
    if updates:
        batch_update(token, creds, updates)
    print(f"完成：已确保标题字段存在，并清空 {len(updates)} 条记录的标题/内容角度")
    return 0


if __name__ == "__main__":
    sys.exit(main())
