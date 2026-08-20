"""W28 Day2 前端自测脚本（临时）：验证三页构建 + 数据处理函数 + 生成器逻辑。"""

# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ---- 1) 应用构建 ----
from app import build_app

app = build_app()
assert app is not None
print("[1] build_app OK")

# ---- 2) 对话页数据处理 ----
from pages import chat

d = {
    "columns": ["订单号", "金额"],
    "rows": [["SO-1", 100], ["SO-2", 200]],
    "sql": "SELECT * FROM orders LIMIT 2",
    "elapsed": 0.5,
    "insights": ["GMV 上升"],
}
out = chat._to_dataframe(d)
assert out["headers"] == ["订单号", "金额"], out
assert out["data"] == [["SO-1", 100], ["SO-2", 200]]
print("[2] _to_dataframe OK")

md = chat._render_citations(["SCM-PUR-004"], ["SCM-PUR-004"], "rag", {"passed": True})
assert "SCM-PUR-004" in md and "通过" in md
md2 = chat._render_citations([], [], "rag", {"passed": True})
assert "无引用" in md2
print("[3] _render_citations OK")

# ---- 3) 审批页数据处理 ----
from pages import approvals
from scm_client.models import ApprovalItem

items = [
    ApprovalItem(
        approval_id="a1",
        session_id="s1",
        operation="update_order",
        order_id="SO-1",
        diff=[{"field": "amount", "old": 1, "new": 2}],
        reason="改金额",
        status="pending",
        created_at="2026-09-01",
    )
]
rows = approvals._pending_to_table(items)
assert rows[0][0] == "a1" and rows[0][1] == "update_order"
d2 = approvals._format_diff(items[0].diff)
assert "amount" in d2
print("[4] approvals 数据函数 OK")

# ---- 4) 对话生成器逻辑（MockTransport 模拟 SSE）----
import gradio as gr
import httpx


def _fake_stream(request: httpx.Request) -> httpx.Response:
    events = [
        {"type": "progress", "node": "retrieve", "data": {"result": "混合检索命中 3 个候选"}},
        {"type": "message", "role": "assistant", "content": "供应商准入需", "delta": True},
        {"type": "message", "role": "assistant", "content": "要营业执照。", "delta": True},
        {"type": "message", "role": "assistant", "content": "", "delta": False},
        {
            "type": "citations",
            "citations": ["SCM-PUR-004"],
            "retrieved_docs": ["SCM-PUR-004"],
            "validation": {"passed": True, "retries": 0, "degraded": False, "warning": ""},
            "session_id": "s-test",
        },
        {"type": "done"},
    ]
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    return httpx.Response(200, content=body.encode("utf-8"))


import json  # noqa: E402

mock_client = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(_fake_stream))
scm = __import__("scm_client").ScmCopilot(
    base_url="http://mock", api_key="sk-test", client=mock_client
)

# 模拟生成器：直接跑 chat_stream 主循环
history = []
answer = ""
citations = []
table = None
for ev in scm.chat_stream("供应商准入需要哪些资质？"):
    if ev.type == "message":
        answer += ev.delta
    elif ev.type == "citations":
        citations = ev.data.get("citations") or []
    elif ev.type == "data_table":
        table = ev.data
assert answer == "供应商准入需要营业执照。", answer
assert citations == ["SCM-PUR-004"], citations
print("[5] 对话 SSE 流式事件循环 OK ->", answer)

# ---- 6) 审批页 _decide 逻辑（MockTransport）----
approvals_json = {
    "approvals": [
        {
            "approval_id": "a1",
            "session_id": "s1",
            "operation": "update_order",
            "order_id": "SO-1",
            "diff": [{"field": "amount", "old": 1, "new": 2}],
            "reason": "改金额",
            "status": "pending",
            "created_at": "2026-09-01",
        }
    ],
    "total": 1,
}


def _fake_approvals(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/ops/approvals") and request.method == "GET":
        return httpx.Response(200, json=approvals_json)
    return httpx.Response(200, json={"ok": True, "reply": "已改单"})


mock2 = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(_fake_approvals))
scm2 = __import__("scm_client").ScmCopilot(base_url="http://mock", api_key="sk-test", client=mock2)
pending = scm2.approvals.list_pending()
assert len(pending) == 1 and pending[0].approval_id == "a1"
resp = scm2.approvals.decide("a1", "approve", reason="平台放行", session_id="s1")
assert resp["ok"] is True
print("[6] 审批 list_pending/decide 事件循环 OK")

print("\n=== W28 Day2 前端自测 ALL PASS ===")
