r"""baseline 探活：正常态健康检查 + 业务端点（演练对照基线）。

用法：
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/baseline_check.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import httpx  # noqa: E402

BASE = "https://localhost:18443"


def login(client: httpx.Client) -> str:
    r = client.post(f"{BASE}/api/v1/auth/login",
                    json={"username": "admin_t_huadong", "password": "Passw0rd!"},
                    timeout=15)
    if r.status_code != 200:
        raise SystemExit(f"login failed: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def health(client: httpx.Client) -> dict:
    r = client.get(f"{BASE}/health", timeout=10)
    return {"code": r.status_code, "body": r.json()}


def kb_chat(client: httpx.Client, headers: dict, msg: str) -> dict:
    r = client.post(f"{BASE}/api/v1/kb/chat",
                    json={"message": msg, "session_id": "baseline-1"},
                    headers=headers, timeout=30)
    events = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
    return {"code": r.status_code, "events": events[:3], "has_done": any('"done"' in e for e in events)}


def ops_chat(client: httpx.Client, headers: dict, msg: str) -> dict:
    r = client.post(f"{BASE}/api/v1/ops/chat",
                    json={"message": msg, "session_id": "baseline-ops"},
                    headers=headers, timeout=30)
    return {"code": r.status_code, "text": r.text[:300]}


def main() -> None:
    with httpx.Client(verify=False) as client:
        token = login(client)
        headers = {"Authorization": f"Bearer {token}"}
        print("health:", health(client))
        print("kb_chat:", json.dumps(kb_chat(client, headers, "你好呀，你能做什么？"), ensure_ascii=False)[:300])
        print("kb_chat(rag):", json.dumps(kb_chat(client, headers, "采购申请需要经过哪几级审批"), ensure_ascii=False)[:200])
        print("ops_chat:", json.dumps(ops_chat(client, headers, "查一下订单 PO-0001 的状态"), ensure_ascii=False)[:200])
        print("ops_chat(高危改单):", json.dumps(ops_chat(client, headers, "把订单 PO-0002 的金额改成 9500"), ensure_ascii=False)[:200])


if __name__ == "__main__":
    main()
