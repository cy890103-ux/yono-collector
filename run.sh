#!/bin/bash
# ─── run.sh ───
# 拉最新代码 → 立刻运行 YONO Collector
# 用法：./run.sh                     # 默认搜5张（按星期轮换关键词）
#       ./run.sh -k "sneaker" -n 3   # 自定义关键词+数量
#       ./run.sh --list              # 只看飞书记录数
#       ./run.sh --backfill          # 回填空字段

cd "$(dirname "$0")"

# 拉最新
echo "⬇ 拉取 GitHub 最新代码..."
git pull origin main

# 检查 sources.yaml 是否存在
if [ ! -f "config/sources.yaml" ]; then
    echo "⚠️  config/sources.yaml 不存在！"
    echo "   请复制模板并填入真实 Key："
    echo "   cp config/sources.template.yaml config/sources.yaml"
    echo "   然后手动编辑填入 Pexels API Key 和飞书 App ID/Secret"
    exit 1
fi

# 运行脚本（传所有参数）
echo "🚀 开始运行..."
/Users/mac/.workbuddy/binaries/python/envs/default/bin/python3 yono_collector.py "$@"
