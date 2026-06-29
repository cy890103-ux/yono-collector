# YONO Collector 🌿

> Pexels 搜图 → 飞书 Bitable 自动写入，按 OS.md (YONO Playbook v1.0) 标准自动分类、评分、标注。

## 文件结构

```
yono_collector.py         # 主脚本
config/
  keywords.yaml           # 搜索关键词轮换 + 分类映射
  judge.yaml              # 评分/原因/标签标准
  sources.yaml            # ⚠ API Key + 飞书连接（不推 GitHub）
  sources.template.yaml   # 模板（推 GitHub，填入真实 Key 后另存为 sources.yaml）
```

## 快速开始

1. 复制 `config/sources.template.yaml` → `config/sources.yaml`，填入真实 API Key
2. 安装依赖：`pip install requests PyYAML`
3. 运行：

```bash
python3 yono_collector.py                    # 默认搜 5 张
python3 yono_collector.py -k "sneaker" -n 10  # 自定义关键词 + 数量
python3 yono_collector.py --list              # 查看飞书记录数
python3 yono_collector.py --backfill          # 回填已有记录的空字段
```

## 改配置不动代码

| 要改什么 | 改哪个文件 | 说明 |
|---------|-----------|------|
| 搜什么关键词 | `config/keywords.yaml` | 每天轮换哪组词、精确映射 |
| 评分/标签/原因 | `config/judge.yaml` | 六大模块评判标准 |
| API Key / 飞书地址 | `config/sources.yaml` | 密钥配置，不推 GitHub |

## OS.md 标准

- 六大模块：Postcard(30%), Archive(20%), Field Notes(15%), OOTD(15%), Block(10%), Tape(10%)
- Score 1-5：Not YONO → Weak → Usable → Strong → Highly YONO
- Visual Tags 13 项 + Mood Tags 8 项
- 流程：Collect → Judge → Generate → Review → Publish → Review

## 自动执行

Cron 每天 9:00 自动运行：

```bash
0 9 * * * python3 /path/to/yono_collector.py >> yono_collector.log 2>&1
```
