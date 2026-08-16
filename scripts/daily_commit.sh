#!/usr/bin/env bash
# ★ SCM Copilot 每日一键提交脚本（在 Git Bash 中运行，规避 PowerShell GBK 乱码）
# 用法（项目根目录）：
#   bash scripts/daily_commit.sh "W23-D2 平台库五表建模"              # 全部变更
#   bash scripts/daily_commit.sh "修复版" pyproject.toml ci.yml       # 只提交指定路径（每日两版工作流）
#   bash scripts/daily_commit.sh                                      # 自动生成消息（W{周}-D{日} 日期）
#
# 行为：
#   1. 无变更 → 提示并退出（不产生空提交）
#   2. 暂存（给路径则只 add 路径，否则 add -A）+ 提交
#   3. push 到 origin 当前分支
set -euo pipefail

# ---- 0. 参数：commit message + 可选路径 ----
MSG="${1:-}"
shift $(( $# > 0 ? 1 : 0 ))    # 剩余参数 = 路径列表（可为空）

if [ -z "$MSG" ]; then
  # 自动生成周次：W23 起点 2026-08-17（周一），每 7 天 +1，覆盖 W23–W26
  EPOCH="$(date -d 2026-08-17 +%s)"
  TODAY="$(date +%s)"
  WEEK_NUM=$(( 23 + (TODAY - EPOCH) / 86400 / 7 ))
  DAY_NUM="$(date +%u)"        # 周一=1 ... 周日=7，与手册 D1–D7 对齐
  MSG="W${WEEK_NUM}-D${DAY_NUM} 日常提交 $(date +%F)"
fi

# ---- 1. 检查是否有变更 ----
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "== 没有可提交的变更，跳过 =="
  exit 0
fi

# ---- 2. 暂存 + 提交 ----
if [ "$#" -gt 0 ]; then
  echo "== 只提交指定路径：$* =="
  git add -- "$@"
else
  git add -A
fi
git commit -m "$MSG"

# ---- 3. 推送 ----
echo "== push 到 origin =="
git push origin HEAD

echo "== 完成：$MSG =="
