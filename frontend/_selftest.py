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

# ---- 7) 日报页 BI 图表数据函数（W28 Day3）----
from pages import brief

sample_charts = {
    "latest_date": "2026-09-02",
    "points": [
        {"date": "2026-08-27", "gmv": 1000.0, "delay_rate": 8.0},
        {"date": "2026-08-28", "gmv": 2000.0, "delay_rate": 9.0},
        {"date": "2026-09-02", "gmv": 3000.0, "delay_rate": 9.91},
    ],
    "top_suppliers": [
        {"rank": 1, "supplier": "华东A", "gmv": 1800.0},
        {"rank": 2, "supplier": "华北B", "gmv": 1200.0},
    ],
    "sqls": [
        {
            "key": "gmv",
            "question": "昨日订单总金额（GMV）是多少？",
            "sql": "SELECT SUM(amount) AS gmv FROM orders WHERE ...",
        }
    ],
    "baseline_delay_rate": 9.91,
}

# _to_wanyuan 换算（元 → 万元）
assert brief._to_wanyuan(3000.0) == 0.3
assert brief._to_wanyuan(None) is None
assert brief._hover_money(36738101.8) == "36,738,101.80"
print("[7] _to_wanyuan / _hover_money OK")

# GMV 折线：含数据 trace + 昨日标注点
fg = brief.build_gmv_figure(sample_charts)
assert len(fg.data) == 1 and len(fg.layout.annotations) == 1
assert fg.layout.annotations[0].text == "昨日"
assert fg.layout.font.family.startswith("Microsoft YaHei"), "中文字体应生效"
print("[8] build_gmv_figure OK")

# 延迟率折线：含基准虚线（hline 以 shape 存在）
fd = brief.build_delay_figure(sample_charts)
shapes = [s for s in (fd.layout.shapes or []) if s.type == "line"]
assert len(shapes) == 1, "应有 9.91% 基准虚线"
assert shapes[0].y0 == 9.91, "基准虚线与后端 baseline 一致"
print("[9] build_delay_figure OK")

# TOP5 横向柱状：rank1 在顶（y 轴反转）
ft = brief.build_top5_figure(sample_charts)
ys = list(ft.data[0].y)
assert ys == ["华北B", "华东A"], f"rank1 应在顶部, got {ys}"
print("[10] build_top5_figure OK")

# SQL 回溯 + 空态
assert "SELECT SUM(amount)" in brief.render_sqls(sample_charts)
assert "无 SQL" in brief.render_sqls({"sqls": []})
assert brief.render_empty_message(sample_charts) == ""
assert "无日报数据" in brief.render_empty_message({"points": []})
print("[11] render_sqls / render_empty_message OK")

# fetch_charts（httpx MockTransport 模拟 200）
import httpx as _httpx

_FAKE_CHARTS = {
    "latest_date": "2026-09-02",
    "points": [{"date": "2026-09-02", "gmv": 1.0, "delay_rate": 9.91}],
    "top_suppliers": [],
    "sqls": [],
    "baseline_delay_rate": 9.91,
}


def _fake_charts(request: _httpx.Request) -> _httpx.Response:
    if "unreachable" in str(request.url):
        raise _httpx.ConnectError("connection refused", request=request)
    if request.url.path.endswith("/admin/brief/charts"):
        return _httpx.Response(200, json=_FAKE_CHARTS)
    return _httpx.Response(404, text="not found")


_mock_httpx = _httpx.Client(
    base_url="http://mock", transport=_httpx.MockTransport(_fake_charts)
)
_orig_get = brief.httpx.get
brief.httpx.get = lambda url, **kw: _mock_httpx.get(str(url), headers=kw.get("headers") or {})
try:
    got = brief.fetch_charts("https://nginx:443", "sk-test")
    assert got is not None and got["latest_date"] == "2026-09-02"
    bad = brief.fetch_charts("https://unreachable.invalid", "sk-test")
    assert bad is None, "连接失败应返回 None（图整张不挂）"
finally:
    brief.httpx.get = _orig_get
print("[12] fetch_charts OK")

print("\n=== W28 Day2+Day3 前端自测 ALL PASS ===")
