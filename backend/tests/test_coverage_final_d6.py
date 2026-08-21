"""★ W28-D6 覆盖率收尾 III：prompts / eval_nightly 纯逻辑 / cache_warmup 终验。

全部纯逻辑（CI 可跑）：不依赖 DB/模型/Qdrant。
"""

from datetime import date

import pytest

from app.domains.data.prompts import (
    DATA_BASE_DATE,
    SCHEMA_TEXT,
    V2_FEW_SHOT_MAX,
    _build_recalled_schema,
    _select_few_shots,
    build_few_shots,
    build_nl2sql_messages,
    build_nl2sql_messages_v1,
    build_nl2sql_messages_v2,
    estimate_prompt_tokens,
    estimate_tokens,
)

# ==================== data/prompts.py（49% → 补 v1/v2/估算/联动） ====================


def test_schema_text_contains_six_tables():
    for t in ("orders", "order_items", "products", "suppliers", "inventory", "shipments"):
        assert f"{t}（" in SCHEMA_TEXT or f"{t}:" in SCHEMA_TEXT or f"{t}（" in SCHEMA_TEXT


def test_build_few_shots_str_today():
    shots = build_few_shots("2026-08-18")
    assert len(shots) == 7
    assert shots[0]["tables"] == ["orders"]
    # 时间窗示例显式日期（today 驱动）
    assert "created_at >= '2026-07-19'" in shots[2]["sql"]


def test_build_nl2sql_messages_v1_structure():
    msgs = build_nl2sql_messages_v1("华东区域有多少订单？", "2026-08-18")
    assert msgs[0]["role"] == "system"
    assert "SELECT" in msgs[1]["content"] and "华东区域有多少订单" in msgs[1]["content"]
    assert len(msgs) == 2


def test_select_few_shots_linked_join():
    """v2 联动：召回 orders+suppliers → join 供应商示例排最前。"""
    shots = _select_few_shots("2026-08-18", ["orders", "suppliers"])
    assert shots and shots[0]["tables"] == ["orders", "suppliers"]


def test_select_few_shots_single_table_no_join():
    """单表召回 → join 示例全部滤除。"""
    shots = _select_few_shots("2026-08-18", ["orders"])
    assert all(set(fs["tables"]) <= {"orders"} for fs in shots)


def test_build_nl2sql_messages_v2_with_tables():
    msgs = build_nl2sql_messages_v2("各区域订单总金额？", "2026-08-18", tables=["orders"])
    assert msgs[0]["role"] == "system"
    assert len(msgs[1]["content"]) < len(build_nl2sql_messages_v1("q", "2026-08-18")[1]["content"])


def test_build_recalled_schema_single_no_relationships():
    text = _build_recalled_schema(["orders"])
    assert "orders" in text
    # 单表不注入关联关系
    assert "关联" not in text or "## 表关系" not in text


def test_build_nl2sql_messages_default_v1(monkeypatch):
    monkeypatch.delenv("PROMPT_VERSION", raising=False)
    msgs = build_nl2sql_messages("测试问题", "2026-08-18")
    assert msgs[0]["role"] == "system"


def test_build_nl2sql_messages_v2_env(monkeypatch):
    monkeypatch.setenv("PROMPT_VERSION", "v2")
    msgs = build_nl2sql_messages("测试问题", "2026-08-18", tables=["orders"])
    assert msgs[0]["role"] == "system"


def test_estimate_tokens_cjk_and_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("华东区域订单") > 0
    assert estimate_prompt_tokens([{"content": "华东"}, {"content": "abc"}]) > 0


def test_v2_few_shot_max_positive():
    assert V2_FEW_SHOT_MAX >= 1


# ==================== eval_nightly 纯逻辑（44% → 补 is_failed/gauges/目录探测） ====================


class TestEvalNightlyPure:
    def test_is_failed(self):
        from app.platform.scheduler.jobs.eval_nightly import _is_failed

        assert _is_failed(None) is True
        assert _is_failed({"error": "boom"}) is True
        assert _is_failed({"n": 10, "error_rate": 1.0}) is True
        assert _is_failed({"n": 10, "error_rate": 0.1}) is False
        assert _is_failed({"n": 0}) is False  # n=0 不算失败

    def test_find_eval_dir_env_override(self, monkeypatch, tmp_path):
        from app.platform.scheduler.jobs.eval_nightly import _find_eval_dir

        monkeypatch.setenv("SCM_EVAL_DIR", str(tmp_path))
        assert _find_eval_dir() == tmp_path

    def test_find_scripts_dir_env_override(self, monkeypatch, tmp_path):
        from app.platform.scheduler.jobs.eval_nightly import _find_scripts_dir

        monkeypatch.setenv("SCM_SCRIPTS_DIR", str(tmp_path))
        assert _find_scripts_dir() == tmp_path

    def test_push_eval_gauges(self):
        from app.platform.scheduler.jobs.eval_nightly import _push_eval_gauges
        from app.shared.obs import metrics as m

        m.clear()
        _push_eval_gauges(
            {"hit@1": 0.9, "recall@5": 0.8, "citation_accuracy": 0.7, "error_rate": 0.0},
            {"overall": 0.85, "single": 0.9, "join": 0.8, "aggregation": 0.9,
             "count": 100, "rejected": 2},
        )
        rendered = m.render()
        assert "scm_nl2sql_eval_score" in rendered
        assert "scm_rag_eval_score" in rendered
        # None metrics → 不炸
        _push_eval_gauges(None, None)
        m.clear()

    def test_eval_rag_missing_file(self, monkeypatch):
        import app.platform.scheduler.jobs.eval_nightly as en

        monkeypatch.setattr(en, "_RAG_EVAL_FILE", __import__("pathlib").Path("nonexistent.json"))
        import asyncio

        metrics = asyncio.run(en._eval_rag("2026-09-05"))
        assert metrics["error"] == "eval file missing"


# ==================== cache_warmup（41% → 无问题/无 runtime） ====================


class TestCacheWarmup:
    def test_run_no_questions(self, monkeypatch):
        from app.platform.scheduler.jobs import cache_warmup

        async def _none(yesterday, limit):
            return []

        monkeypatch.setattr(cache_warmup, "_yesterday_hot_questions", _none)
        import asyncio

        out = asyncio.run(cache_warmup.run())
        assert out["status"] == "success" and out["candidates"] == 0

    def test_yesterday_hot_questions_no_runtime(self, monkeypatch):
        from app.platform.scheduler import _runtime
        from app.platform.scheduler.jobs import cache_warmup

        old = _runtime.session_factory
        _runtime.session_factory = None
        try:
            import asyncio

            assert asyncio.run(
                cache_warmup._yesterday_hot_questions(date(2026, 9, 4))
            ) == []
        finally:
            _runtime.session_factory = old

    def test_yesterday_hot_questions_query_fail(self, monkeypatch):
        from app.platform.scheduler.jobs import cache_warmup

        class _BoomFactory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(self):
                        raise ConnectionError("db down")

                    async def __aexit__(self, *a):
                        return False

                return _Ctx()

        from app.platform.scheduler import _runtime

        old = _runtime.session_factory
        _runtime.session_factory = _BoomFactory()
        try:
            import asyncio

            assert asyncio.run(
                cache_warmup._yesterday_hot_questions(date(2026, 9, 4))
            ) == []
        finally:
            _runtime.session_factory = old
