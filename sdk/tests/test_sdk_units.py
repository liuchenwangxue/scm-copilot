"""SDK 单元测试：MockTransport 模拟平台，验证 SSE 解析 / 错误映射 / 请求构造。

离线可跑（CI 无需真实平台）：httpx.MockTransport 拦截所有请求。
覆盖（对照手册 Day5 坑）：
- chat_stream：SSE 多行 data 拼接 / 四型事件分发 / delta 便捷取增量 / 流错误
- nl2sql：结果结构 / as_dataframe（pandas 缺失 → 带安装提示的 ScmError）
- errors：Err 契约解析 → ScmError / ScmAuthError / ScmQuotaError(Retry-After)
- approvals：list_pending 解析 / decide 请求体
- 认证头：API Key 以 `Bearer sk-` 发送
"""

import json
import re

import httpx
import pytest

from scm_client import ScmCopilot
from scm_client.errors import ScmAuthError, ScmError, ScmQuotaError


def _sse(events: list[dict]) -> str:
    """构造 SSE 文本：每个事件 `data: {json}\n\n`（后端 _ss 同款格式）。"""
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def _make_client(handler) -> ScmCopilot:
    transport = httpx.MockTransport(handler)
    # 自定义 client 需自带 base_url（SDK 不接管连接管理）
    return ScmCopilot(
        "http://testserver",
        api_key="sk-test",
        client=httpx.Client(base_url="http://testserver", transport=transport),
    )


# ==================== chat_stream：SSE 解析 ====================


def test_chat_stream_parses_sse_events_and_delta():
    """message 增量 + citations + done 事件解析，delta 只对 message 事件有值。"""
    events = [
        {"type": "progress", "node": "retrieve", "data": {"result": "检索 3 候选"}},
        {"type": "message", "role": "assistant", "content": "你好", "delta": True},
        {"type": "message", "role": "assistant", "content": "世界", "delta": True},
        {"type": "citations", "citations": [], "retrieved_docs": []},
        {"type": "done"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kb/chat"
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, text=_sse(events), headers={"content-type": "text/event-stream"})

    client = _make_client(handler)
    got = list(client.chat_stream("你好"))
    assert [e.type for e in got] == ["progress", "message", "message", "citations", "done"]
    # delta 只聚合 message 事件的内容
    assert "".join(e.delta for e in got) == "你好世界"
    assert got[-1].type == "done"


def test_chat_stream_handles_multi_line_data_and_ops_domain():
    """SSE `data:` 多行拼接（一个事件分两行）+ ops 域路径 + approval_request 事件。"""
    payload = {"type": "approval_request", "approval_id": "a1",
               "form": {"operation": "改单", "diff": []}, "session_id": "s1"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/ops/chat"
        body = json.loads(request.content)
        assert body["message"] == "帮我改一下订单"
        # 多行 data：第一行 data: { 开头的 JSON，第二行续接，之后空行
        text = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    client = _make_client(handler)
    events = list(client.chat_stream("帮我改一下订单", session_id="s1", domain="ops"))
    assert len(events) == 1
    assert events[0].type == "approval_request"
    assert events[0].data["approval_id"] == "a1"
    # data_table 事件 delta 为空但 data 可用
    assert events[0].delta == ""


def test_chat_stream_bad_domain_rejected():
    client = _make_client(lambda r: httpx.Response(200))
    with pytest.raises(ValueError):
        list(client.chat_stream("x", domain="bogus"))


# ==================== nl2sql ====================


def test_nl2sql_result_structure_and_sql():
    payload = {
        "table": True, "sql": "SELECT supplier, SUM(amount) gmv FROM orders GROUP BY supplier",
        "columns": ["supplier", "gmv"], "rows": [["华东A", 100.0]],
        "reply": "TOP1 华东A", "question": "TOP 供应商", "elapsed": 12.3,
        "rejected_reason": None, "insights": ["华东A 领先"], "session_id": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/data/query"
        return httpx.Response(200, json=payload)

    client = _make_client(handler)
    result = client.nl2sql("TOP 供应商")
    assert result.table is True
    assert "GROUP BY supplier" in result.sql
    assert result.rows == [["华东A", 100.0]]
    assert result.insights == ["华东A 领先"]


def test_nl2sql_as_dataframe_with_or_without_pandas():
    payload = {
        "table": True, "sql": "SELECT name FROM suppliers", "columns": ["name"],
        "rows": [["华东A"], ["华南B"]], "reply": "ok", "question": "q",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _make_client(handler)
    try:
        import pandas  # noqa: F401
    except ImportError:
        HAS_PANDAS = False
    else:
        HAS_PANDAS = True

    result = client.nl2sql("q", as_dataframe=True)
    if HAS_PANDAS:
        assert result.df is not None
        assert list(result.df.columns) == ["name"]
        assert len(result.df) == 2
    else:
        # 无 pandas：抛带安装提示的错误（不静默失败）
        with pytest.raises(ScmError) as ei:
            client.nl2sql("q", as_dataframe=True)
        assert "pandas" in str(ei.value)


# ==================== 错误映射 ====================


def test_error_401_maps_to_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "AUTH_401", "message": "invalid token", "trace_id": "t1"})

    client = _make_client(handler)
    with pytest.raises(ScmAuthError) as ei:
        client.nl2sql("q")
    assert ei.value.code == "AUTH_401"
    assert ei.value.trace_id == "t1"


def test_error_429_maps_to_quota_error_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"code": "QUOTA_429", "message": "api key rate limit exceeded", "trace_id": "t2"},
            headers={"Retry-After": "12"},
        )

    client = _make_client(handler)
    with pytest.raises(ScmQuotaError) as ei:
        client.nl2sql("q")
    assert ei.value.code == "QUOTA_429"
    assert ei.value.retry_after == 12


# ==================== approvals ====================


def test_approvals_list_pending_and_decide():
    items = [
        {"approval_id": "a1", "session_id": "s1", "operation": "修改订单",
         "order_id": "PO-0001", "diff": [{"field": "amount", "before": 100, "after": 200}],
         "reason": "改金额", "status": "pending", "created_at": "2026-09-04T09:00:00+08:00"},
    ]
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/ops/approvals":
            return httpx.Response(200, json={"approvals": items, "total": 1})
        if request.method == "POST" and request.url.path == "/api/v1/ops/approval":
            seen.append((request.url.path, json.dumps(json.loads(request.content))))
            return httpx.Response(200, json={"ok": True, "approval_id": "a1", "decision": "approve",
                                             "reply": "已批准并执行"})
        return httpx.Response(404)

    client = _make_client(handler)
    pending = client.approvals.list_pending()
    assert len(pending) == 1
    assert pending[0].approval_id == "a1"
    assert pending[0].session_id == "s1"
    assert pending[0].diff[0]["field"] == "amount"

    out = client.approvals.decide("a1", "approve", reason="平台放行", session_id="s1")
    assert out["ok"] is True
    body = json.loads(seen[0][1])
    assert body == {"approval_id": "a1", "decision": "approve", "reason": "平台放行", "session_id": "s1"}


def test_approvals_decide_rejects_bad_action():
    client = _make_client(lambda r: httpx.Response(200))
    with pytest.raises(ValueError):
        client.approvals.decide("a1", "maybe")


# ==================== 认证头与辅助 ====================


def test_api_key_sent_as_bearer_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    client = ScmCopilot(
        "http://testserver",
        api_key="sk-abc123",
        client=httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(handler)),
    )
    client._request("GET", "/healthz")
    assert captured["auth"] == "Bearer sk-abc123"
