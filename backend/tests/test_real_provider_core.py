"""W27 Day5 覆盖率冲刺 I：real provider 核心成功路径（provider.py 26% → 60%+）。

覆盖手册 Day5（real 系列 ≥60%）：
- __init__：models / model_override / Key 缺失报错
- generate / generate_json：成功路径（_post_chat 注入）+ 降级路径
- stream：成功（SSE 行收集）+ 失败降级
- _build_messages / _extract_json / _switch_model / _get_client / _mock_async_answer
- 网络 IO（_post_chat 真实 HTTP）属薄壳，走"文档化接受"清单（面试口径）
"""
import json

import httpx
import pytest

from app.shared.llm.real import cost as _cost
from app.shared.llm.real import model_pool as _pool
from app.shared.llm.real import obs as _obs_mod
from app.shared.llm.real.provider import RealLLMProvider


def _ok_payload(content: str):
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def provider(monkeypatch):
    import app.shared.config as config

    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://localhost:9999")
    monkeypatch.setattr(_pool, "_model_pool_state", {"idx": 0, "models": ["m1", "m2"]})
    monkeypatch.setattr(_pool, "_pool_models", lambda: ["m1", "m2"])
    monkeypatch.setattr(_pool, "_load_active_model", lambda: None)
    monkeypatch.setattr(_cost, "_log_cost", lambda *a, **kw: None)
    monkeypatch.setattr(_obs_mod._obs, "generation", lambda *a, **kw: None)
    monkeypatch.setattr("app.shared.obs.otel.get_tracer", lambda: None)
    return RealLLMProvider(models=["m1", "m2"], degrade_to_mock=True)


class TestInit:
    def test_init_with_models(self, provider):
        assert provider.models == ["m1", "m2"]
        assert provider.model == "m1"
        assert provider.degrade_to_mock is True

    def test_init_model_override(self, monkeypatch):
        import app.shared.config as config

        monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
        monkeypatch.setattr(config, "LLM_BASE_URL", "http://x")
        p = RealLLMProvider(model_override="mx")
        assert p.models == ["mx"] and p.model == "mx"

    def test_init_requires_api_key(self, monkeypatch):
        import app.shared.config as config

        monkeypatch.setattr(config, "LLM_API_KEY", "")
        monkeypatch.setattr(config, "LLM_BASE_URL", "http://x")
        with pytest.raises(RuntimeError):
            RealLLMProvider(models=["m1"])

    def test_init_default_pool_path(self, monkeypatch):
        """无 models/override → 走模型池（reorder + 全局 idx），degrade 读环境变量。"""
        import app.shared.config as config

        monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
        monkeypatch.setattr(config, "LLM_BASE_URL", "http://x")
        monkeypatch.setattr(config, "LLM_DEGRADE_TO_MOCK", "0")
        monkeypatch.setattr(_pool, "_pool_models", lambda: ["m1", "m2"])
        monkeypatch.setattr(_pool, "_load_active_model", lambda: None)
        p = RealLLMProvider()
        assert p.models == ["m1", "m2"]
        assert p.degrade_to_mock is False, 'LLM_DEGRADE_TO_MOCK=0 应关闭降级'

    def test_init_empty_models_raises(self, monkeypatch):
        import app.shared.config as config

        monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
        monkeypatch.setattr(config, "LLM_BASE_URL", "http://x")
        monkeypatch.setattr(_pool, "_pool_models", lambda: [])
        with pytest.raises(RuntimeError):
            RealLLMProvider()


class TestInternals:
    def test_get_client_reused(self, provider):
        c1 = provider._get_client()
        c2 = provider._get_client()
        assert c1 is c2
        assert isinstance(c1, httpx.AsyncClient)

    def test_switch_model(self, provider):
        provider._switch_model()
        assert provider.model == "m2"
        assert _pool._model_pool_state["idx"] == 1

    def test_build_messages_no_context(self, provider):
        msgs = [{"role": "user", "content": "hi"}]
        assert provider._build_messages(msgs, {}) == msgs

    def test_build_messages_with_override(self, provider):
        msgs = [{"role": "user", "content": "hi"}]
        out = provider._build_messages(
            msgs,
            {
                "retrieval_context": [{"doc_id": "d1", "text": "t"}],
                "system_prompt_override": "自定义系统提示",
            },
        )
        assert out[0] == {"role": "system", "content": "自定义系统提示"}
        assert out[1:] == msgs

    def test_build_messages_builtin_prompt(self, provider):
        msgs = [{"role": "user", "content": "hi"}]
        out = provider._build_messages(msgs, {"retrieval_context": [{"doc_id": "d1", "text": "t"}]})
        assert out[0]["role"] == "system"
        assert "d1" in out[0]["content"]


class TestExtractJson:
    def test_plain_json(self, provider):
        assert provider._extract_json('{"a": 1}') == {"a": 1}

    def test_json_with_noise(self, provider):
        text = '思考中……\n```json\n{"a": 1, "b": "2"}\n```\n结尾'
        assert provider._extract_json(text) == {"a": 1, "b": "2"}

    def test_missing_json_raises(self, provider):
        with pytest.raises(ValueError):
            provider._extract_json("没有 JSON 对象")


class TestGenerate:
    async def test_generate_success(self, provider, monkeypatch):
        async def _ok(payload, tag):
            return _ok_payload("你好，供应链助手"), {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }

        monkeypatch.setattr(provider, "_post_chat", _ok)
        out = await provider.generate([{"role": "user", "content": "你好"}])
        assert out == "你好，供应链助手"

    async def test_generate_degrades_on_error(self, provider, monkeypatch):
        async def _boom(payload, tag):
            raise RuntimeError("HTTP 500: internal error")

        monkeypatch.setattr(provider, "_post_chat", _boom)
        out = await provider.generate([{"role": "user", "content": "hi"}])
        assert out.startswith("[WARNING]")


class TestGenerateJson:
    async def test_success(self, provider, monkeypatch):
        async def _ok(payload, tag):
            return _ok_payload(json.dumps({"answer": "a", "citations": ["d1"]})), {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            }

        monkeypatch.setattr(provider, "_post_chat", _ok)
        parsed = await provider.generate_json([{"role": "user", "content": "q"}], schema={})
        assert parsed["answer"] == "a" and parsed["citations"] == ["d1"]

    async def test_retry_after_bad_json(self, provider, monkeypatch):
        calls = {"n": 0}

        async def _post(payload, tag):
            calls["n"] += 1
            if calls["n"] == 1:
                return _ok_payload("截断的半截 JSON"), {}
            return _ok_payload('{"answer": "ok"}'), {}

        monkeypatch.setattr(provider, "_post_chat", _post)
        assert await provider.generate_json([{"role": "user", "content": "q"}], schema={}) == {
            "answer": "ok"
        }
        assert calls["n"] == 2, "JSON 解析失败应重试一次"

    async def test_twice_bad_raises(self, provider, monkeypatch):
        async def _post(payload, tag):
            return _ok_payload("截断的半截 JSON"), {}

        monkeypatch.setattr(provider, "_post_chat", _post)
        with pytest.raises(ValueError):
            await provider.generate_json([{"role": "user", "content": "q"}], schema={})


class TestStream:
    def _fake_client(self, lines):
        class _Resp:
            status_code = 200

            def __init__(self, lines):
                self._lines = lines

            async def aiter_lines(self):
                for ln in self._lines:
                    yield ln

        class _CM:
            def __init__(self, resp):
                self._resp = resp

            async def __aenter__(self):
                return self._resp

            async def __aexit__(self, *a):
                return False

        class _Client:
            def stream(self, method, path, json=None):
                return _CM(_Resp(lines))

        return _Client()

    async def test_stream_success(self, provider, monkeypatch):
        lines = [
            'data: {"choices": [{"delta": {"content": "你"}}]}',
            'data: {"usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}',
            "data: [DONE]",
        ]
        monkeypatch.setattr(provider, "_get_client", lambda: self._fake_client(lines))
        chunks = [ch async for ch in provider.stream([{"role": "user", "content": "q"}])]
        assert "".join(chunks) == "你"

    async def test_stream_failure_degrades(self, provider, monkeypatch):
        def _boom():
            raise RuntimeError("conn refused")

        monkeypatch.setattr(provider, "_get_client", _boom)
        chunks = [ch async for ch in provider.stream([{"role": "user", "content": "q"}])]
        assert "[WARNING]" in "".join(chunks)

    async def test_stream_http_error_degrades(self, provider, monkeypatch):
        """stream 响应非 200 → 抛 _ProviderError → 降级 mock（fail-open）。"""

        class _Resp:
            status_code = 401

            async def aread(self):
                return b"unauthorized"

            async def aiter_lines(self):
                yield ""

        class _CM:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        class _Client:
            def stream(self, method, path, json=None):
                return _CM()

        monkeypatch.setattr(provider, "_get_client", lambda: _Client())
        chunks = [ch async for ch in provider.stream([{"role": "user", "content": "q"}])]
        assert "[WARNING]" in "".join(chunks)

    async def test_stream_skips_bad_json_chunks(self, provider, monkeypatch):
        lines = [
            'data: {"choices": [{"delta": {"content": "你"}}]}',
            "data: not-json{{{",
            'data: {"choices": [{"delta": {"content": "好"}}]}',
            "data: [DONE]",
        ]
        monkeypatch.setattr(provider, "_get_client", lambda: self._fake_client(lines))
        chunks = [ch async for ch in provider.stream([{"role": "user", "content": "q"}])]
        assert "".join(chunks) == "你好", "坏 JSON 行应跳过，不中断流"


class TestPostChat:
    """_post_chat：重试 + 模型切换 + 成功返回（注入假 httpx client，不走网络）。"""

    @staticmethod
    def _resp(status=200, text="ok", data=None):
        class _R:
            def __init__(self):
                self.status_code = status
                self.text = text
                self._data = data or {
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }

            def json(self):
                return self._data

        return _R()

    @pytest.fixture(autouse=True)
    def _fast_retry(self, monkeypatch):
        """重试延迟归零，测试不睡指数退避。"""
        monkeypatch.setattr("app.shared.llm.real.provider.BASE_DELAY", 0.0)

    async def test_success_returns_body_and_usage(self, provider, monkeypatch):
        calls = {"n": 0}

        class _Client:
            async def post(self, path, json=None):
                calls["n"] += 1
                return TestPostChat._resp()

        monkeypatch.setattr(provider, "_get_client", lambda: _Client())
        monkeypatch.setattr(_pool, "_save_active_model", lambda model: None)
        body, usage = await provider._post_chat({"model": "m1", "messages": []}, "generate")
        assert body["choices"][0]["message"]["content"] == "hi"
        assert usage["prompt_tokens"] == 1
        assert provider.model == "m1"
        assert calls["n"] == 1

    async def test_quota_error_switches_model(self, provider, monkeypatch):
        calls = {"n": 0}

        class _Client:
            async def post(self, path, json=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return TestPostChat._resp(status=429, text="quota exhausted")
                return TestPostChat._resp(data={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

        monkeypatch.setattr(provider, "_get_client", lambda: _Client())
        monkeypatch.setattr(_pool, "_save_active_model", lambda model: None)
        body, _u = await provider._post_chat({"model": "m1", "messages": []}, "generate")
        assert body["choices"][0]["message"]["content"] == "ok"
        assert provider.model == "m2", "额度耗尽应立即切下一个模型（不浪费重试次数）"

    async def test_transient_error_retries_then_success(self, provider, monkeypatch):
        calls = {"n": 0}

        class _Client:
            async def post(self, path, json=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise httpx.ConnectError("conn refused")
                return TestPostChat._resp(data={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

        monkeypatch.setattr(provider, "_get_client", lambda: _Client())
        monkeypatch.setattr(_pool, "_save_active_model", lambda model: None)
        body, _u = await provider._post_chat({"model": "m1", "messages": []}, "generate")
        assert body["choices"][0]["message"]["content"] == "ok"
        assert calls["n"] == 2, "瞬时错误应单模型内重试一次后成功"

    async def test_all_models_fail_raises(self, provider, monkeypatch):
        class _Client:
            async def post(self, path, json=None):
                raise httpx.ConnectError("conn refused")

        monkeypatch.setattr(provider, "_get_client", lambda: _Client())
        monkeypatch.setattr(_pool, "_save_active_model", lambda model: None)
        with pytest.raises(RuntimeError, match="所有模型"):
            await provider._post_chat({"model": "m1", "messages": []}, "generate")


class TestMockAsyncAnswer:
    async def test_creates_mock_on_demand(self):
        p = RealLLMProvider.__new__(RealLLMProvider)
        p._mock = None
        out = await p._mock_async_answer([{"role": "user", "content": "q"}], {})
        assert isinstance(out, str) and out
        assert p._mock is not None
