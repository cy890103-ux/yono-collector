#!/usr/bin/env python3
"""Delete a Feishu Bitable field by name."""

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


def get_feishu_credentials(sources_cfg):
    return {
        "app_id": sources_cfg["feishu"]["app_id"],
        "app_secret": sources_cfg["feishu"]["app_secret"],
        "app_token": sources_cfg["feishu"]["app_token"],
        "table_id": sources_cfg["feishu"]["table_id"],
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


def list_fields(token, creds):
    resp = requests.get(
        f"{API}/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 100},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"读取字段失败: {data}")
    return data.get("data", {}).get("items", [])


def delete_field(token, creds, field_id):
    resp = requests.delete(
        f"{API}/bitable/v1/apps/{creds['app_token']}/tables/{creds['table_id']}/fields/{field_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"删除字段失败: {data}")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("field_name", nargs="?", default="Curiosity Point｜好奇心点")
    parser.add_argument("--list", action="store_true", help="Only list fields")
    args = parser.parse_args()

    creds = get_feishu_credentials(load_sources())
    token = get_token(creds)
    fields = list_fields(token, creds)

    if args.list:
        for field in fields:
            print(f"{field.get('field_name')}\\t{field.get('field_id')}")
        return 0

    matches = [field for field in fields if field.get("field_name") == args.field_name]
    if not matches:
        print(f"字段不存在，无需删除: {args.field_name}")
        return 0

    field = matches[0]
    field_id = field["field_id"]
    print(f"删除字段: {args.field_name} ({field_id})")
    delete_field(token, creds, field_id)
    print("删除完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
