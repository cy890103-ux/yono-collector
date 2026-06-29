#!/bin/bash
# YONO Collector — 每日自动抓取
# Postcard 5 条 → Archive 5 条 → Tape 5 条

SCRIPT_DIR="/Users/mac/WorkBuddy/2026-06-28-11-57-10"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG="$SCRIPT_DIR/logs/daily_$(date +%Y-%m-%d).log"

mkdir -p "$SCRIPT_DIR/logs"

echo "====== $(date '+%Y-%m-%d %H:%M:%S') 开始每日抓取 ======" >> "$LOG"

cd "$SCRIPT_DIR"

echo "--- Postcard ---" >> "$LOG"
$PYTHON yono_collector.py --keyword "warm quiet morning light emotion" --count 5 --images 4 >> "$LOG" 2>&1 || echo "⚠️ Postcard 出错，继续下一分类" >> "$LOG"

echo "--- Archive ---" >> "$LOG"
$PYTHON yono_collector.py --keyword "vintage brand design photography" --count 5 --images 4 >> "$LOG" 2>&1 || echo "⚠️ Archive 出错，继续下一分类" >> "$LOG"

echo "--- Tape ---" >> "$LOG"
$PYTHON yono_collector.py --keyword "ambient indie soundtrack" --count 5 --images 4 >> "$LOG" 2>&1 || echo "⚠️ Tape 出错，继续下一分类" >> "$LOG"

echo "--- 删除放弃记录 ---" >> "$LOG"
$PYTHON yono_collector.py --delete-rejected >> "$LOG" 2>&1 || echo "⚠️ 删除放弃记录出错" >> "$LOG"

echo "====== $(date '+%Y-%m-%d %H:%M:%S') 完成 ======" >> "$LOG"
