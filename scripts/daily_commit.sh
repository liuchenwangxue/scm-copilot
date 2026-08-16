#!/usr/bin/env bash
# ★ W23 每日一键提交脚本
# 用法（在项目根目录）：
#   bash scripts/daily_commit.sh "W23-D2 平台库五表建模"     # 带自定义消息
#   bash scripts/daily_commit.sh                             # 自动生成消息（W23-D{n} 日期）
#
# 行为：
#   1. git add -A（包含新增/修改/删除）
#   2. 无变更 → 提示并退出（不产生空提交）
#   3. 提交并 push 到 origin 当前分支
set -euo pipefail

# ---- 0. 参数：commit message ----
MSG="${1:-}"
if [ -z "$MSG" ]; then
  # 自动生成：从本周目录名推导周次（W23），日期做后缀
  WEEK="W23"
  TODAY="$(date +%Y-%m-%d)"
  MSG="${WEEK} 日常提交 ${TODAY}"
fi

# ---- 1. 检查是否有变更 ----
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "== 没有可提交的变更，跳过 =="
  exit 0
fi

# ---- 2. 暂存 + 提交 ----
git add -A
git commit -m "$MSG"

# ---- 3. 推送 ----
echo "== push 到 origin =="
git push origin HEAD

echo "== 完成：$MSG =="
