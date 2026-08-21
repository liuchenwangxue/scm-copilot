"""★ W28-D6 覆盖率收尾 II：纯逻辑洼地一次补齐（B4 终验）。

覆盖清单（对应 coverage_acceptance_w27d5 终审 + 手册 Day6 第 3 条）：
- mock_provider：冲突检测 / 空上下文 / 单/多来源 / generate/stream/generate_json
- logger：JsonFormatter（dict/str/exc/request_id）/ setup 幂等 / log_event / read_logs / 中间件
- cache：QueryCache 命中/过期/删除/fail-open 内存兜底
- retry_policy：is_retryable_http 分类 / RetryPolicy.run 重试救回 / degrade_chain 各级
- redis_client：RedisClient 全操作 fail-open 路径（注入假 client 抛异常）
- store/retriever：SCMStore 假 Qdrant 客户端（query 过滤/重试/upsert/collection 管理）
- hybrid_retriever：rrf/weighted 融合纯函数 + 租户过滤 + chunk_meta 容错
- feedback_store：submit/review/flow_back 全流程（tmp_path 隔离）

全部纯逻辑（CI 可跑，不依赖 MySQL/Redis/Qdrant 真实服务）。
"""

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest

# ==================== mock_provider（33% → 冲突/多源分支） ====================


class TestMockProvider:
    def test_empty_context_no_fabrication(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        r = p._answer_from_context([])
        assert "未检索到" in r["answer"] and r["citations"] == []

    def test_single_doc_answer(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        r = p._answer_from_context(
            [
                {
                    "doc_id": "SCM-PUR-001_招标",
                    "section_path": "s",
                    "text": "第1条 采购金额超过100万必须招标。",
                }
            ]
        )
        assert r["citations"] == ["SCM-PUR-001_招标"]
        assert "根据《SCM-PUR-001_招标》" in r["answer"]

    def test_multi_doc_answer(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        ctx = [
            {
                "doc_id": "SCM-PUR-001_招标",
                "section_path": "s",
                "text": "第1条 金额超100万必须招标。",
            },
            {"doc_id": "SCM-SUP-001_供应商", "section_path": "s", "text": "第2条 供应商需持证。"},
        ]
        r = p._answer_from_context(ctx)
        assert "2 份资料综合" in r["answer"]
        assert len(r["citations"]) == 2

    def test_conflict_detection(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        # 同一主题前缀（PUR-001 / PUR-002）：数字集合「有交集（30天）但不全同」→ 冲突
        ctx = [
            {
                "doc_id": "SCM-PUR-001",
                "section_path": "s",
                "text": "结算账期 30 天，违约金 1000 元。",
            },
            {
                "doc_id": "SCM-PUR-002",
                "section_path": "s",
                "text": "结算账期 30 天，违约金 2000 元。",
            },
        ]
        r = p._answer_from_context(ctx)
        assert "口径不同" in r["answer"]
        assert len(r["citations"]) >= 2

    def test_conflict_disjoint_not_conflict(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        # 数字集合完全不相交 = 不同事项，不算冲突
        ctx = [
            {"doc_id": "SCM-PUR-001", "section_path": "s", "text": "账期 30 天。"},
            {"doc_id": "SCM-INV-001", "section_path": "s", "text": "库存 100 件。"},
        ]
        r = p._answer_from_context(ctx)
        assert "口径不同" not in r["answer"]

    async def test_generate_and_stream(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        msg = [{"role": "user", "content": "采购流程"}]
        out = await p.generate(msg, retrieval_context=[])
        assert isinstance(out, str) and out
        gen = p.stream(msg, retrieval_context=[])
        text = "".join([c async for c in gen])
        assert text

    async def test_generate_json_contract(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        p = MockLLMProvider()
        out = await p.generate_json(
            [{"role": "user", "content": "x"}], {"type": "object"}, retrieval_context=[]
        )
        assert set(out) == {"answer", "citations"}

    def test_estimate_tokens(self):
        from app.shared.llm.mock_provider import _estimate_tokens

        assert _estimate_tokens([{"content": "你好世界"}]) >= 1
        assert _estimate_tokens([{"content": ""}]) == 1

    def test_inc_usage_failure_ignored(self, monkeypatch):
        """metrics 导入/调用失败 → mock 仍返回（观测旁路）。"""
        from app.shared.llm import mock_provider

        monkeypatch.setattr(mock_provider, "_inc_mock_usage", lambda *a, **kw: None)
        p = mock_provider.MockLLMProvider()
        out = asyncio.run(p.generate([{"role": "user", "content": "x"}]))
        assert out


# ==================== logger（43% → formatter/中间件/读回） ====================


class TestLogger:
    @pytest.fixture(autouse=True)
    def _reset_handler(self):
        """reset 模块级 _handler：setup() 幂等导致多次调用只认第一个路径。"""
        import app.shared.obs.logger as logmod

        old = logmod._handler
        logmod._handler = None
        yield
        logmod._handler = old

    def test_json_formatter_dict_msg(self, tmp_path):
        from app.shared.obs.logger import JsonFormatter, setup

        setup("test", log_path=tmp_path / "s.log")
        import logging

        lg = logging.getLogger("app.test_fmt")
        rec = lg.makeRecord(
            "app.test_fmt", logging.INFO, __file__, 1, {"event": "test", "foo": "bar"}, None, None
        )
        line = JsonFormatter().format(rec)
        d = json.loads(line)
        assert d["event"] == "test" and d["foo"] == "bar" and d["level"] == "info"

    def test_json_formatter_str_msg_and_request_id(self):
        import logging

        from app.shared.obs.logger import JsonFormatter

        lg = logging.getLogger("app.test_str")
        rec = lg.makeRecord(
            "app.test_str", logging.WARNING, __file__, 1, "plain message", None, None
        )
        rec.request_id = "req-1"
        rec.trace_id = "abcd"
        d = json.loads(JsonFormatter().format(rec))
        assert d["message"] == "plain message"
        assert d["request_id"] == "req-1" and d["trace_id"] == "abcd"

    def test_log_event_and_read_logs(self, tmp_path):
        from app.shared.obs.logger import clear_logs, get_logger, log_event, read_logs, setup

        setup("test", log_path=tmp_path / "e.log")
        lg = get_logger("event")
        log_event(lg, "my_event", level="warning", n=1, request_id="r1")
        recs = read_logs(tmp_path / "e.log")
        assert recs and recs[-1]["event"] == "my_event"
        assert recs[-1]["n"] == 1 and recs[-1]["request_id"] == "r1"
        clear_logs(tmp_path / "e.log")
        assert read_logs(tmp_path / "e.log") == []

    def test_read_logs_missing_and_corrupt(self, tmp_path):
        from app.shared.obs.logger import read_logs

        assert read_logs(tmp_path / "nope.jsonl") == []
        p = tmp_path / "bad.jsonl"
        p.write_text("not-json\n", encoding="utf-8")
        assert read_logs(p) == []  # JSONDecodeError 跳过

    async def test_middleware_non_http_pass_through(self):
        """非 http scope → 原样透传。"""
        from app.shared.obs.logger import RequestLogMiddleware

        async def app(scope, receive, send):
            await send({"type": "lifespan.startup"})

        mw = RequestLogMiddleware(app, enabled=False)
        sent = []

        async def _send(m):
            sent.append(m)

        await mw({"type": "lifespan"}, None, _send)
        assert sent == [{"type": "lifespan.startup"}]

    async def test_middleware_http_logs_and_injects(self, tmp_path):
        """http 请求 → X-Request-Id 注入 + http_request 日志落盘。"""
        from app.shared.obs.logger import RequestLogMiddleware, read_logs, setup

        setup("test-mw", log_path=tmp_path / "mw.log")

        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = RequestLogMiddleware(app, enabled=True)
        messages = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            messages.append(msg)

        await mw({"type": "http", "method": "GET", "path": "/api/v1/health"}, receive, send)
        assert messages[0]["type"] == "http.response.start"
        headers = dict(messages[0]["headers"])
        assert b"X-Request-Id" in headers
        recs = read_logs(tmp_path / "mw.log")
        assert recs and recs[-1]["event"] == "http_request"
        assert recs[-1]["status"] == 200 and recs[-1]["method"] == "GET"

    async def test_middleware_disabled_no_log(self, tmp_path):
        from app.shared.obs.logger import RequestLogMiddleware, read_logs, setup

        setup("test-mw2", log_path=tmp_path / "mw2.log")

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def _send(m):
            pass

        mw = RequestLogMiddleware(app, enabled=False)
        await mw({"type": "http", "method": "GET", "path": "/p"}, None, _send)
        assert read_logs(tmp_path / "mw2.log") == []


# ==================== cache（35% → QueryCache 全路径） ====================


class _FakeRC:
    """内存 redis：支持 get/set/delete + 可注入故障。

    故障语义对齐真实 RedisClient 的 fail-open：挂掉时返回 None/False，
    **不抛异常**（QueryCache 依赖 redis_client 层 fail-open，不自己 try）。
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.fail = False

    def get(self, key):
        if self.fail:
            return None  # fail-open：读不到 = miss
        return self.store.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            return False  # fail-open：写失败返回 False，不抛
        self.store[key] = value
        return True

    def delete(self, key):
        if self.fail:
            return False
        self.store.pop(key, None)
        return True


class TestQueryCache:
    def test_build_key_deterministic(self):
        from app.shared.reliability.cache import QueryCache

        a = QueryCache.build_key("order", {"id": 1})
        b = QueryCache.build_key("order", {"id": 1})
        assert a == b and a != QueryCache.build_key("order", {"id": 2})

    def test_set_get_hit(self):
        from app.shared.reliability.cache import QueryCache

        c = QueryCache(ttl=60, redis_client=_FakeRC())
        c.set({"v": 1}, "order", "1")
        val, hit = c.get("order", "1")
        assert hit and val == {"v": 1}

    def test_get_miss(self):
        from app.shared.reliability.cache import QueryCache

        c = QueryCache(ttl=60, redis_client=_FakeRC())
        assert c.get("nope") == (None, False)

    def test_delete(self):
        from app.shared.reliability.cache import QueryCache

        c = QueryCache(ttl=60, redis_client=_FakeRC())
        c.set({"v": 1}, "k")
        c.delete("k")
        assert c.get("k") == (None, False)

    def test_redis_corrupt_json_falls_to_memory(self):
        from app.shared.reliability.cache import QueryCache

        rc = _FakeRC()
        rc.store["cache:corrupt"] = "{bad json"
        c = QueryCache(ttl=60, redis_client=rc, use_memory=True)
        # 内存没有 → miss（不抛）
        assert c.get("corrupt") == (None, False)

    def test_redis_fail_memory_fallback(self):
        from app.shared.reliability.cache import QueryCache

        rc = _FakeRC()
        c = QueryCache(ttl=60, redis_client=rc, use_memory=True)
        c.set({"v": "m"}, "x")  # 写进内存
        rc.fail = True  # Redis 挂
        val, hit = c.get("x")
        assert hit and val == {"v": "m"}  # 内存兜底
        # 写失败静默（内存仍生效）
        c.set({"v": 2}, "y")
        assert c.get("y") == ({"v": 2}, True)

    def test_memory_expire(self, monkeypatch):
        from app.shared.reliability.cache import QueryCache

        rc = _FakeRC()
        c = QueryCache(ttl=60, redis_client=rc, use_memory=True)
        c.set({"v": 1}, "k")
        rc.store.clear()  # redis 层 miss，只测内存过期路径
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 120)  # 时间快进 120s
        assert c.get("k") == (None, False)  # 内存过期 → miss

    def test_no_memory_no_redis(self):
        from app.shared.reliability.cache import QueryCache

        rc = _FakeRC()
        rc.fail = True
        c = QueryCache(ttl=60, redis_client=rc, use_memory=False)
        assert c.get("k") == (None, False)


# ==================== retry_policy（58% → 重试救回/降级链分级） ====================


class TestRetryPolicy:
    def test_is_retryable_http_classification(self):
        from app.shared.reliability.circuit_breaker import CircuitOpenError
        from app.shared.reliability.retry_policy import is_retryable_http

        assert is_retryable_http(RuntimeError("timeout")) is True
        assert is_retryable_http(RuntimeError("connection refused")) is True
        assert is_retryable_http(RuntimeError("500 Internal")) is True
        assert is_retryable_http(RuntimeError("404 not found")) is False
        assert is_retryable_http(RuntimeError("409 conflict")) is False
        assert is_retryable_http(CircuitOpenError("open")) is False

    def test_retry_recovers(self, monkeypatch):
        from app.shared.reliability.retry_policy import RetryPolicy

        calls = {"n": 0}

        def func():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("timeout")
            return "ok"

        monkeypatch.setattr("time.sleep", lambda s: None)
        rp = RetryPolicy(
            max_retries=3, base_delay=0.01, retryable=lambda e: isinstance(e, TimeoutError)
        )
        result, attempts, recovered = rp.run(func)
        assert result == "ok" and attempts == 3 and recovered is True

    def test_retry_exhausted_raises(self, monkeypatch):
        from app.shared.reliability.retry_policy import RetryPolicy

        monkeypatch.setattr("time.sleep", lambda s: None)

        def func():
            raise TimeoutError("always")

        rp = RetryPolicy(max_retries=2, base_delay=0.01)
        with pytest.raises(RuntimeError, match="仍失败"):
            rp.run(func)

    def test_non_retryable_raises_immediately(self):
        from app.shared.reliability.retry_policy import RetryPolicy

        rp = RetryPolicy(max_retries=2, retryable=lambda e: False)
        with pytest.raises(ValueError):
            rp.run(lambda: (_ for _ in ()).throw(ValueError("400")))

    def test_on_retry_callback(self, monkeypatch):
        from app.shared.reliability.retry_policy import RetryPolicy

        seen = []
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls = {"n": 0}

        def func():
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("t")
            return "ok"

        rp = RetryPolicy(
            max_retries=1, base_delay=0.01, on_retry=lambda a, d, e: seen.append((a, d))
        )
        rp.run(func)
        assert len(seen) == 1

    def test_degrade_chain_primary_ok(self):
        from app.shared.reliability.retry_policy import degrade_chain

        result, meta = degrade_chain(lambda: "primary", retries=1, base_delay=0.01)
        assert result == "primary" and meta["level"] == 0 and meta["degraded"] is False

    def test_degrade_chain_fallback_used(self, monkeypatch):
        from app.shared.reliability.retry_policy import degrade_chain

        monkeypatch.setattr("time.sleep", lambda s: None)
        result, meta = degrade_chain(
            primary=lambda: (_ for _ in ()).throw(TimeoutError("down")),
            backups=(lambda: (_ for _ in ()).throw(TimeoutError("down")),),
            fallback=lambda: "fallback",
            retries=1,
            base_delay=0.01,
        )
        assert result == "fallback" and meta["fallback_used"] is True

    def test_degrade_chain_circuit_open_continues(self, monkeypatch):
        from app.shared.reliability.circuit_breaker import CircuitOpenError
        from app.shared.reliability.retry_policy import degrade_chain

        monkeypatch.setattr("time.sleep", lambda s: None)
        result, meta = degrade_chain(
            primary=lambda: (_ for _ in ()).throw(CircuitOpenError("open")),
            backups=(lambda: "backup",),
            retries=1,
            base_delay=0.01,
        )
        assert result == "backup" and meta["level"] == 1 and meta["degraded"] is True

    def test_degrade_chain_biz_error_raises(self):
        """业务错误（非熔断）→ 不降级直接透传（如实告诉用户）。"""
        from app.shared.reliability.retry_policy import degrade_chain

        with pytest.raises(ValueError, match="400 bad"):
            degrade_chain(
                primary=lambda: (_ for _ in ()).throw(ValueError("400 bad")),
                retryable=lambda e: False,
            )


# ==================== redis_client（66% → 操作 fail-open） ====================


class TestRedisClientFailOpen:
    def _client(self):
        from app.shared.reliability.redis_client import RedisClient

        return RedisClient(url="redis://fake:6379/0", enabled=True, timeout=0.01)

    def test_all_ops_fail_open(self, monkeypatch):
        c = self._client()

        class _Boom:
            def ping(self):
                raise ConnectionError("down")

            def set(self, *a, **kw):
                raise ConnectionError("down")

            def get(self, *a, **kw):
                raise ConnectionError("down")

            def delete(self, *a, **kw):
                raise ConnectionError("down")

            def eval(self, *a, **kw):
                raise ConnectionError("down")

            def sadd(self, *a, **kw):
                raise ConnectionError("down")

            def smembers(self, *a, **kw):
                raise ConnectionError("down")

            def srem(self, *a, **kw):
                raise ConnectionError("down")

            def ttl(self, *a, **kw):
                raise ConnectionError("down")

            def incrbyfloat(self, *a, **kw):
                raise ConnectionError("down")

            def scan(self, *a, **kw):
                raise ConnectionError("down")

        monkeypatch.setattr(c, "_connect", lambda: _Boom())
        assert c.ping() is False
        assert c.available is False
        assert c.set("k", "v") is False
        assert c.set_nx("k", "v") is False
        assert c.get("k") is None
        assert c.delete("k") is False
        assert c.delete_if_equals("k", "v") is False
        assert c.sadd("s", "m") == 0
        assert c.smembers("s") == set()
        assert c.srem("s", "m") == 0
        assert c.delete_many(["a", "b"]) == 0
        assert c.ttl("k") == -2
        assert c.incrbyfloat("k", 1.0) is None
        assert c.eval("lua", 1, "k") is None
        assert c.scan_keys("cache:*") == []

    def test_disabled(self):
        from app.shared.reliability.redis_client import RedisClient

        c = RedisClient(enabled=False)
        assert c.ping() is False and c.available is False

    def test_delete_many_empty(self):
        c = self._client()
        assert c.delete_many([]) == 0


# ==================== store / retriever（12%/0% → 假 Qdrant 客户端） ====================


class _FakeQdrantClient:
    """内存版 QdrantClient：upsert/query/collection 管理原语。"""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.points: dict[str, list] = {}
        self.fail_query = False
        self.create_kwargs: dict = {}

    def collection_exists(self, name):
        return name in self.collections

    def create_collection(self, collection_name, vectors_config=None, **kw):
        self.collections[collection_name] = {}
        self.points.setdefault(collection_name, [])
        self.create_kwargs = {"vectors_config": vectors_config, **kw}
        return None

    def delete_collection(self, name):
        self.collections.pop(name, None)
        self.points.pop(name, None)
        return True

    def get_collection(self, name):
        class _C:
            points_count = len(self.points.get(name, []))

            class config:
                params = type(
                    "P",
                    (),
                    {
                        "vectors": type(
                            "V",
                            (),
                            {
                                "size": 4,
                                "distance": "COSINE",
                                "hnsw_config": type("H", (), {"m": 16, "ef_construct": 200})(),
                            },
                        )()
                    },
                )()

        return _C()

    def upsert(self, collection_name, points, wait=True):
        self.points.setdefault(collection_name, []).extend(points)
        return None

    def query_points(self, collection_name, query, query_filter, limit, with_payload, **kw):
        if self.fail_query:
            raise ConnectionError("502 Bad Gateway")
        out = []
        for p in self.points.get(collection_name, []):
            if query_filter is not None and not self._match(query_filter, p.payload):
                continue
            out.append(
                type(
                    "P",
                    (),
                    {
                        "payload": p.payload,
                        "score": float(p.vector[0]),
                    },
                )()
            )
        return type("R", (), {"points": out[:limit]})()

    def _match(self, qfilter, payload):
        for cond in qfilter.must or []:
            key = cond.key
            value = cond.match.value
            if payload.get(key) != value:
                return False
        return True

    def scroll(self, collection_name, limit, offset=None, with_payload=True, with_vectors=False):
        return self.points.get(collection_name, []), None


class TestStore:
    def _store(self, monkeypatch, **kw):
        from app.shared.rag import store as store_mod

        fake = _FakeQdrantClient()
        monkeypatch.setattr(store_mod, "QdrantClient", lambda *a, **k: fake)
        s = store_mod.SCMStore(collection="test_coll", url="http://fake:6333")
        return s, fake

    def test_create_and_info(self, monkeypatch):
        from app.shared.rag.store import SCMStore

        fake = _FakeQdrantClient()
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: fake)
        s = SCMStore(collection="t")
        info = s.create_collection(dim=4)
        assert info["collection"] == "t" and info["dim"] == 4
        # 已存在不 overwrite → 直接返回 info
        assert s.create_collection(dim=4)["points_count"] == 0
        # overwrite 重建
        s.create_collection(dim=4, overwrite=True)
        assert s.info()["collection"] == "t"

    def test_upsert_and_query_filter(self, monkeypatch):
        from app.shared.rag.store import SCMStore

        fake = _FakeQdrantClient()
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: fake)
        s = SCMStore(collection="t")
        s.create_collection(dim=4)
        chunks = [
            {"chunk_id": "c1", "doc_id": "SCM-PUR-001", "section_path": "s", "text": "招标条款"},
            {"chunk_id": "c2", "doc_id": "SCM-INV-001", "section_path": "s", "text": "库存条款"},
        ]
        vectors = [np.array([0.1, 0.2, 0.3, 0.4]), np.array([0.5, 0.6, 0.7, 0.8])]
        n = s.upsert_with_vectors(chunks, vectors, tenant_id="t_huadong")
        assert n == 2
        # tenant filter
        hits = s.query([0.1, 0.2, 0.3, 0.4], top_k=5, tenant_id="t_huadong")
        assert len(hits) == 2
        hits = s.query([0.1, 0.2, 0.3, 0.4], top_k=5, tenant_id="t_huabei")
        assert len(hits) == 0  # 租户过滤
        assert hits == []

    def test_query_retry_recovers(self, monkeypatch):
        """502 → 指数退避重试后恢复（默认 retries=3）。"""
        from app.shared.rag.store import SCMStore

        fake = _FakeQdrantClient()
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: fake)
        s = SCMStore(collection="t")
        s.create_collection(dim=4)
        s.upsert_with_vectors(
            [{"chunk_id": "c1", "doc_id": "SCM-PUR-001", "text": "x"}],
            [np.array([0.1, 0.2, 0.3, 0.4])],
        )
        calls = {"n": 0}

        # 第一次抛瞬时 502，之后正常（不依赖 fail_query 标志）
        def _flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("502 Bad Gateway")
            return _FakeQdrantClient.query_points(fake, *a, **kw)

        fake.query_points = _flaky
        monkeypatch.setattr("time.sleep", lambda s: None)
        hits = s.query([0.1, 0.2, 0.3, 0.4])
        assert len(hits) == 1  # 重试救回
        assert calls["n"] == 2

    def test_query_non_retryable_raises(self, monkeypatch):
        from app.shared.rag.store import SCMStore

        fake = _FakeQdrantClient()
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: fake)
        s = SCMStore(collection="t")

        def _bad(*a, **kw):
            raise ValueError("400 bad request")

        fake.query_points = _bad
        with pytest.raises(ValueError):
            s.query([0.1] * 4)

    def test_delete_collection(self, monkeypatch):
        from app.shared.rag.store import SCMStore

        fake = _FakeQdrantClient()
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: fake)
        s = SCMStore(collection="t")
        s.create_collection(dim=4)
        s.delete_collection()
        assert fake.collections == {}


class TestRetriever:
    def test_retrieve_and_top_docs(self, monkeypatch):
        from app.shared.rag.retriever import Retriever

        store = _FakeQdrantClient()
        store.create_collection("scm_kb_v1", dim=4)
        store.upsert(
            "scm_kb_v1",
            [
                type(
                    "P",
                    (),
                    {
                        "payload": {
                            "chunk_id": "c1",
                            "doc_id": "SCM-PUR-001",
                            "section_path": "s",
                            "topic": "采购",
                            "tenant_id": "",
                            "text": "条款A",
                        },
                        "vector": [0.9],
                    },
                ),
                type(
                    "P",
                    (),
                    {
                        "payload": {
                            "chunk_id": "c2",
                            "doc_id": "SCM-PUR-001",
                            "section_path": "s",
                            "topic": "采购",
                            "tenant_id": "",
                            "text": "条款B",
                        },
                        "vector": [0.8],
                    },
                ),
                type(
                    "P",
                    (),
                    {
                        "payload": {
                            "chunk_id": "c3",
                            "doc_id": "SCM-INV-001",
                            "section_path": "s",
                            "topic": "库存",
                            "tenant_id": "",
                            "text": "条款C",
                        },
                        "vector": [0.7],
                    },
                ),
            ],
        )
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: store)
        monkeypatch.setattr(
            "app.shared.rag.retriever.Embedder",
            lambda **k: type("E", (), {"embed_query": lambda self, q: np.array([0.1] * 4)})(),
        )
        r = Retriever(collection="scm_kb_v1")
        hits = r.retrieve("采购", top_k=5)
        assert len(hits) == 3
        docs = r.retrieve_top_docs("采购", top_k=5)
        assert docs == ["SCM-PUR-001", "SCM-INV-001"]  # 去重保序

    def test_retrieve_tenant_routes_to_shard(self, monkeypatch):
        """tenant_id 非空 → 路由到分片 collection（sharding.collection_for）。"""
        from app.shared.rag import sharding
        from app.shared.rag.retriever import Retriever

        coll = sharding.collection_for("t_huadong", shards=4)
        store = _FakeQdrantClient()
        store.create_collection(coll, dim=4)
        store.upsert(
            coll,
            [
                type(
                    "P",
                    (),
                    {
                        "payload": {
                            "chunk_id": "c1",
                            "doc_id": "SCM-PUR-001",
                            "section_path": "s",
                            "topic": "",
                            "tenant_id": "t_huadong",
                            "text": "A",
                        },
                        "vector": [0.9],
                    },
                ),
            ],
        )
        monkeypatch.setattr("app.shared.rag.store.QdrantClient", lambda *a, **k: store)
        monkeypatch.setattr(
            "app.shared.rag.retriever.Embedder",
            lambda **k: type("E", (), {"embed_query": lambda self, q: np.array([0.1] * 4)})(),
        )
        r = Retriever(collection="scm_kb_v1")
        hits = r.retrieve("采购", top_k=5, tenant_id="t_huadong")
        assert len(hits) == 1


# ==================== hybrid_retriever 融合纯函数（65% 补加权/租户） ====================


class TestHybridFuse:
    def _bm25(self, chunks, monkeypatch):
        """构造 BM25Index 最小实例（跳过 jieba 分词复杂度用纯词面）。"""
        from app.shared.rag.hybrid_retriever import BM25Index

        idx = BM25Index(chunks)
        # 直接构造 tokenized（规避 jieba 依赖不确定性）
        idx.tokenized = [[c["text"][:1]] for c in chunks]
        from rank_bm25 import BM25Okapi

        idx.bm25 = BM25Okapi(idx.tokenized)
        idx.chunk_index = {c["chunk_id"]: i for i, c in enumerate(chunks)}
        idx._index_tenants()
        return idx

    def test_rrf_fuse_order_and_source(self):
        from app.shared.rag.hybrid_retriever import HybridRetriever

        r = HybridRetriever.__new__(HybridRetriever)
        fused = r._rrf_fuse({"a": 0, "b": 1}, {"b": 0, "a": 2}, top_k=2)
        assert fused[0]["chunk_id"] == "b"  # b 两路 rank 都高
        assert fused[0]["source"] == "both"
        assert fused[1]["source"] == "both"

    def test_weighted_fuse_normalizes(self):
        from app.shared.rag.hybrid_retriever import HybridRetriever

        r = HybridRetriever.__new__(HybridRetriever)
        r.alpha = 0.5
        fused = r._weighted_fuse({"a": 0.9, "b": 0.1}, {"b": 8.0, "c": 2.0}, top_k=3)
        cids = [f["chunk_id"] for f in fused]
        assert "a" in cids and "b" in cids and "c" in cids
        # b 两路都有 → source=both
        b = next(f for f in fused if f["chunk_id"] == "b")
        assert b["source"] == "both"

    def test_weighted_fuse_flat_scores(self):
        """归一化除零防御：两路分数相等 → 全 0，不炸。"""
        from app.shared.rag.hybrid_retriever import HybridRetriever

        r = HybridRetriever.__new__(HybridRetriever)
        r.alpha = 0.5
        fused = r._weighted_fuse({"a": 1.0, "b": 1.0}, {"b": 5.0, "c": 5.0}, top_k=3)
        assert len(fused) == 3

    def test_bm25_search_tenant_filter(self):
        from app.shared.rag.hybrid_retriever import BM25Index

        chunks = [
            {"chunk_id": "c1", "text": "招标条款", "tenant_id": "t_huadong"},
            {"chunk_id": "c2", "text": "招标条款", "tenant_id": "t_huabei"},
            {"chunk_id": "c3", "text": "库存条款"},
        ]
        idx = BM25Index(chunks)
        idx.tokenized = [["招"], ["招"], ["库"]]
        from rank_bm25 import BM25Okapi

        idx.bm25 = BM25Okapi(idx.tokenized)
        idx.chunk_index = {c["chunk_id"]: i for i, c in enumerate(chunks)}
        idx._index_tenants()
        hits = idx.search("招", top_k=5, tenant_id="t_huadong")
        assert [h["chunk_id"] for h in hits] == ["c1"]
        # 租户语料为空 → 返回 []（不跨租户泄露）
        assert idx.search("招", top_k=5, tenant_id="t_west") == []

    def test_bm25_not_built_raises(self):
        from app.shared.rag.hybrid_retriever import BM25Index

        idx = BM25Index([])
        with pytest.raises(RuntimeError, match="未构建"):
            idx.search("x")

    def test_chunk_meta_missing_fallback(self):
        from app.shared.rag.hybrid_retriever import HybridRetriever

        r = HybridRetriever.__new__(HybridRetriever)
        r.chunk_meta = {}
        meta = r._chunk_meta("ghost")
        assert meta["chunk_id"] == "ghost" and meta["doc_id"] == ""


# ==================== feedback_store（22% → 全流程） ====================


class TestFeedbackStore:
    def test_submit_and_review_flow(self, tmp_path):
        from app.domains.kb.feedback.feedback_store import FeedbackStore
        from app.shared.rag.parser.registry import SUPPORTED_EXT  # noqa: F401  # 确保 parser 已导入

        fs = FeedbackStore(path=tmp_path / "fb.jsonl")
        rec = fs.submit(
            user_id="u1",
            question="采购流程?",
            action="correction",
            corrected_answer="正确回答",
            correct_doc_ids=["D1"],
        )
        assert rec["status"] == "pending" and rec["source"] == "feedback"
        assert fs.pending()[0]["feedback_id"] == rec["feedback_id"]
        # 审核通过
        reviewed = fs.review(rec["feedback_id"], approved=True, reviewer="admin")
        assert reviewed["status"] == "approved"
        assert fs.pending() == []
        # 不存在 → None
        assert fs.review("nope", True) is None

    def test_submit_invalid_action_raises(self, tmp_path):
        from app.domains.kb.feedback.feedback_store import FeedbackStore

        fs = FeedbackStore(path=tmp_path / "fb.jsonl")
        with pytest.raises(ValueError):
            fs.submit(user_id="u", question="q", action="bogus")
        with pytest.raises(ValueError):
            fs.submit(user_id="u", question="q", action="correction", corrected_answer=" ")

    def test_flow_back_to_eval(self, tmp_path):
        from app.domains.kb.feedback.feedback_store import FeedbackStore

        qa = tmp_path / "qa.json"
        qa.write_text(
            json.dumps(
                [{"id": "q1", "question": "旧问题", "answer": "旧答", "source_doc_ids": []}]
            ),
            encoding="utf-8",
        )
        fs = FeedbackStore(path=tmp_path / "fb.jsonl", qa_eval_file=qa)
        r1 = fs.submit(
            user_id="u",
            question="新问题?",
            action="correction",
            corrected_answer="新答",
            correct_doc_ids=["D9"],
        )
        fs.review(r1["feedback_id"], True)
        # flow_back 写 v2（独立文件——EVAL_V2_FILE 固定，monkeypatch 到 tmp）
        from app.domains.kb.feedback import feedback_store as fbmod

        fbmod.EVAL_V2_FILE = tmp_path / "v2.json"
        out = fs.flow_back_to_eval()
        assert out["flowed_back"] == 1 and out["base_count"] == 1
        v2 = json.loads(fbmod.EVAL_V2_FILE.read_text(encoding="utf-8"))
        assert any(q["source"] == "feedback" for q in v2)

    def test_stats_and_reset(self, tmp_path):
        from app.domains.kb.feedback.feedback_store import FeedbackStore

        fs = FeedbackStore(path=tmp_path / "fb.jsonl")
        fs.submit(user_id="u", question="q", action="like")
        fs.submit(user_id="u", question="q2", action="dislike")
        st = fs.stats()
        assert st["total"] == 2 and st["by_action"]["like"] == 1
        fs.reset()
        assert fs.list_all() == []
        assert fs.stats()["total"] == 0
