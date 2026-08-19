r"""演练一辅助：MySQL 挂时受保护端点行为观察。

用法（MySQL 已 docker stop 后）：
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/mysql_down_check.py
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
        probes = [
            ("ops_chat_查单", "POST", "/api/v1/ops/chat",
             {"message": "查一下订单 PO-0001 的状态", "session_id": "drill-1"}),
            ("ops_chat_改单", "POST", "/api/v1/ops/chat",
             {"message": "把订单 PO-0002 的金额改成 9500", "session_id": "drill-2"}),
            ("kb_chat", "POST", "/api/v1/kb/chat",
             {"message": "你好呀，你能做什么？", "session_id": "drill-3"}),
            ("ops_approvals", "GET", "/api/v1/ops/approvals", None),
        ]
        for name, method, url, body in probes:
            try:
                r = c.request(method, f"{BASE}{url}", json=body, headers=h)
                print(f"{name}: {r.status_code} {r.text[:200]}")
            except Exception as e:
                print(f"{name}: EXC {type(e).__name__} {str(e)[:80]}")


if __name__ == "__main__":
    main()
