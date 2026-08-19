"""W27 Day4：SDK 自动退避重试单测（MockTransport，离线可跑）。

覆盖（对应手册 Day4 上午第 2 条三序列）：
1. [429 Retry-After:1 → 200]：重试发生、总共 2 次请求、最终拿到 200 结果
2. 429 无 Retry-After → 立即抛（不猜服务端，不空转 sleep）
3. 连续 3 次 429 → 抛 ScmQuotaError（max_retries 用尽）
4. 5xx → 指数退避重试后成功；持续 5xx → 抛 ScmServerError
5. auto_retry=False → 一次 429 直接抛（行为开关）
6. Retry-After >30 → 不重试（避免放大雪崩，尊重服务端保守信号）

sleep 用 monkeypatch 置空：只断言"是否重试/退避分支"，不真等时间。
"""

import json

import httpx
import pytest

from scm_client import ScmCopilot, ScmQuotaError, ScmServerError


def _make_client(handler, **kw) -> ScmCopilot:
    transport = httpx.MockTransport(handler)
    return ScmCopilot(
        "http://testserver",
        api_key="sk-test",
        client=httpx.Client(base_url="http://testserver", transport=transport),
        **kw,
    )


def test_retry_on_429_follows_retry_after_then_succeeds(monkeypatch):
    """[429 Retry-After:1 → 200]：重试 1 次，共 2 次请求，第二次成功。"""
    monkeypatch.setattr("scm_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                json={"code": "QUOTA_429", "message": "slow down", "trace_id": "t1"},
                headers={"Retry-After": "1"},
            )
        return httpx.Response(200, json={"table": True, "sql": "SELECT 1", "columns": ["a"], "rows": []})

    client = _make_client(handler)
    result = client.nl2sql("q")
    assert calls["n"] == 2, "429 应重试 1 次"
    assert result.table is True


def test_429_without_retry_after_raises_immediately(monkeypatch):
    """429 无 Retry-After → 立即抛 ScmQuotaError，不空转退避。"""
    monkeypatch.setattr("scm_client.time.sleep", lambda s: pytest.fail("不应 sleep"))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"code": "QUOTA_429", "message": "limit", "trace_id": "t2"})

    client = _make_client(handler)
    with pytest.raises(ScmQuotaError) as ei:
        client.nl2sql("q")
    assert calls["n"] == 1, "无 Retry-After 不应重试"
    assert ei.value.code == "QUOTA_429"


def test_429_retry_after_over_30_not_retried(monkeypatch):
    """Retry-After >30 → 不重试（尊重服务端保守信号，避免放大雪崩）。"""
    monkeypatch.setattr("scm_client.time.sleep", lambda s: pytest.fail("不应 sleep"))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, json={"code": "QUOTA_429", "message": "backoff long", "trace_id": "t3"},
            headers={"Retry-After": "120"},
        )

    client = _make_client(handler)
    with pytest.raises(ScmQuotaError):
        client.nl2sql("q")
    assert calls["n"] == 1


def test_consecutive_429_exhausts_retries_then_raises(monkeypatch):
    """连续 3 次 429（max_retries=2 用尽）→ 抛 ScmQuotaError。"""
    monkeypatch.setattr("scm_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, json={"code": "QUOTA_429", "message": "still limited", "trace_id": "t4"},
            headers={"Retry-After": "1"},
        )

    client = _make_client(handler)
    with pytest.raises(ScmQuotaError):
        client.nl2sql("q")
    assert calls["n"] == 3, f"应重试 2 次共 3 次请求，实际 {calls['n']}"


def test_5xx_retried_with_backoff_then_succeeds(monkeypatch):
    """5xx → 指数退避重试；[500 → 200] 重试 1 次成功。"""
    sleeps: list[float] = []
    monkeypatch.setattr("scm_client.time.sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"code": "INTERNAL_500", "message": "boom", "trace_id": "t5"})
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)
    client._request("GET", "/healthz")
    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert 0 <= sleeps[0] < 8, f"退避应在 [0,8) 内，实际 {sleeps[0]}"


def test_consecutive_5xx_raises_scm_server_error(monkeypatch):
    """持续 5xx → max_retries 用尽后抛 ScmServerError。"""
    monkeypatch.setattr("scm_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"code": "SERVICE_UNAVAILABLE_503", "message": "down", "trace_id": "t6"})

    client = _make_client(handler)
    with pytest.raises(ScmServerError) as ei:
        client._request("GET", "/healthz")
    assert calls["n"] == 3
    assert ei.value.status_code == 503


def test_auto_retry_false_raises_on_first_429(monkeypatch):
    """auto_retry=False → 429 直接抛，不重试（调用方自行处理）。"""
    monkeypatch.setattr("scm_client.time.sleep", lambda s: pytest.fail("不应 sleep"))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429, json={"code": "QUOTA_429", "message": "limit", "trace_id": "t7"},
            headers={"Retry-After": "1"},
        )

    client = _make_client(handler, auto_retry=False)
    with pytest.raises(ScmQuotaError):
        client.nl2sql("q")
    assert calls["n"] == 1


def test_4xx_not_retried():
    """非 429 的 4xx（如 400 参数错误）不重试——重试也不会变好。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"code": "BAD_REQUEST_400", "message": "bad params", "trace_id": "t8"})

    client = _make_client(handler)
    with pytest.raises(Exception) as ei:
        client.nl2sql("q")
    assert calls["n"] == 1
    assert ei.value.status_code == 400
