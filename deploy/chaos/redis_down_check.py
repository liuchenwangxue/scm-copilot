r"""演练二辅助：Redis 挂时 fail-open 降级行为观察。

预期（W26 Day2 手册）：
- /health 仍 200（db=up，Redis 非 health 判定项）
- 幂等 claim/complete → fail-open 降 sqlite（幂等语义仍成立）
- 查询缓存 → 降级内存 dict（响应可用）
- 分布式锁 / API Key 令牌桶 → 放行打 WARNING（配额软约束）
- 调度 leader 锁 → fail-open 放行（任务幂等兜底）

用法（Redis 已 docker stop 后）：
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/redis_down_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import httpx  # noqa: E402

BASE = "https://localhost:18443"

TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxIiwidXNlcm5hbWUiOiJhZG1pbl90X2h1YWRvbmciLCJ0ZW5hbnRfaWQiOiJ0X2h1YWRvbmciLC"
    "J0eXBlIjoiYWNjZXNzIiwianRpIjoiNzJlM2U4MGE5YjkxNGZmMThhYWMwNmE3MWNjNjhhOWQiLCJpYXQiOjE"
    "3ODcwOTgyOTQsImV4cCI6MTc4NzA5OTE5NCwicGVybWlzc2lvbnMiOlsia2I6Y2hhdCIsImtiOmZlZWRiYWNr"
    "Iiwib3BzOnRvb2w6ZXhlY3V0ZSIsIm9wczphcHByb3ZhbDptYW5hZ2UiLCJkYXRhOm5sMnNxbCIsImFkbWlu"
    "OnNjaGVkdWxlcjptYW5hZ2UiLCJhZG1pbjphcGlrZXk6bWFuYWdlIiwiYWRtaW46YXVkaXQ6dmlldyJdfQ."
    "ypMbMQOGKj_QlQDzQ9-2-8-GFENoEBoIRf15Qe9uovQ"
)


def main() -> None:
    with httpx.Client(verify=False, timeout=25) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        # 1) health：应仍 200（Redis 不在 health 判定项）
        r = c.get(f"{BASE}/health")
        print(f"health: {r.status_code} {r.text[:120]}")

        # 2) 幂等链路（ops 改单走审批 → 执行，幂等 claim 在 Redis；挂 → SQLite 兜底）
        r = c.post(f"{BASE}/api/v1/ops/chat",
                   json={"message": "把订单 PO-0005 的金额改成 8800", "session_id": "drill-redis-1"},
                   headers=h)
        print(f"ops_chat改单: {r.status_code} {r.text[:220]}")

        # 3) 查询/会话（touch_conversation 写 MySQL；cache 在 Redis 挂时走内存）
        r = c.post(f"{BASE}/api/v1/kb/chat",
                   json={"message": "你好", "session_id": "drill-redis-2"},
                   headers=h)
        print(f"kb_chat: {r.status_code} {r.text[:220]}")

        # 4) 调度面板（admin 权限；scheduler 若依赖 Redis leader 锁 → fail-open）
        r = c.get(f"{BASE}/api/v1/admin/scheduler/jobs", headers=h)
        print(f"admin_scheduler_jobs: {r.status_code} {r.text[:150]}")

        # 5) API Key 令牌桶（若有 key：Redis 挂 → fail-open 放行）——此处用 429 端点探测
        r = c.get(f"{BASE}/api/v1/auth/me", headers=h)
        print(f"auth_me: {r.status_code} {r.text[:100]}")


if __name__ == "__main__":
    main()
