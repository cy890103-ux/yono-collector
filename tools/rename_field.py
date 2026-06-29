#!/usr/bin/env python3
"""Rename a Feishu Bitable field."""

import argparse
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old_name")
    parser.add_argument("new_name")
    args = parser.parse_args()

    creds = get_creds()
    token = get_token(creds)
    fields = list_fields(token, creds)

    if any(field.get("field_name") == args.new_name for field in fields):
        print(f"目标字段已存在，无需重命名: {args.new_name}")
        return 0

    matches = [field for field in fields if field.get("field_name") == args.old_name]
    if not matches:
        print(f"源字段不存在: {args.old_name}")
        return 1

    field = matches[0]
    field_id = field["field_id"]
    request_json(
        "PUT",
        f"/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields/{field_id}",
        token,
        json={
            "field_name": args.new_name,
            "type": field.get("type", 1),
        },
    )
    print(f"已重命名字段: {args.old_name} -> {args.new_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
