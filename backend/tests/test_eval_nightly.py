"""W25 Day3 eval_nightly 测试：偏离计算/分位数（纯逻辑）+ NL2SQL 评测/落库（integration）。"""

import pytest
import pytest_asyncio

from app.platform.scheduler.jobs.eval_nightly import _baseline_deviation, _pct

pytestmark = pytest.mark.integration


# ==================== 跨 loop 防护 ====================


@pytest_asyncio.fixture(autouse=True)
async def _dispose_executor_engine():
    """每个用例后释放只读沙箱 engine（防跨 loop 复用——CI pytest-asyncio 1.6.x 严格校验）。

    ★ 与 test_executor.py 的 `_dispose_engine` 同模式：本文件的 integration 用例
    （test_eval_nl2sql_mock_metrics）经 eval_nl2sql 链路创建 `_ExecutorEngine` 模块级单例，
    若不释放，后续 test_executor 的用例在新 loop 复用旧 loop 的 engine → RuntimeError。
    """
    yield
    from app.domains.data.executor import dispose_engine

    await dispose_engine()


# ==================== 纯逻辑 ====================


def test_pct_basic():
    assert _pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 6.0
    assert _pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10.0
    assert _pct([], 0.95) == 0.0


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """仅支持 execute(...).all() 的假 session（_baseline_deviation 只用到这个）。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_baseline_deviation_degraded():
    """今日 0.80 vs 近 7 日均值 0.90 → 劣化 -10pp > 5pp → degraded=True。"""
    rows = [( {"hit@1": v}, ) for v in [0.90, 0.91, 0.89, 0.90, 0.92, 0.88, 0.90]]
    dev = await _baseline_deviation(_FakeSession(rows), "rag", "2026-09-02", {"hit@1": 0.80})
    assert dev["degraded"] is True
    assert dev["delta_pp"] == pytest.approx(-10.0)
    assert dev["samples"] == 7


@pytest.mark.asyncio
async def test_baseline_deviation_ok():
    rows = [( {"hit@1": 0.90}, ) for _ in range(7)]
    dev = await _baseline_deviation(_FakeSession(rows), "rag", "2026-09-02", {"hit@1": 0.92})
    assert dev["degraded"] is False
    assert dev["delta_pp"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_baseline_deviation_no_history():
    """无历史记录 → 不算偏离（degraded=False, samples=0）。"""
    dev = await _baseline_deviation(_FakeSession([]), "rag", "2026-09-02", {"hit@1": 0.90})
    assert dev["degraded"] is False
    assert dev["samples"] == 0


@pytest.mark.asyncio
async def test_baseline_deviation_nl2sql_uses_overall():
    rows = [( {"overall": 0.95}, ), ({"overall": 0.97}, )]
    dev = await _baseline_deviation(_FakeSession(rows), "nl2sql", "2026-09-02", {"overall": 0.96})
    assert dev["vs_7d_avg"] == pytest.approx(0.96)
    assert dev["delta_pp"] == pytest.approx(0.0)


# ==================== integration：NL2SQL 评测与落库 ====================


@pytest_asyncio.fixture
async def runtime():
    """设置模块级运行时上下文（session_factory），用完还原。

    ★ W27-D6 (B10)：_runtime 已是 RuntimeContext 对象——测试文件顶部 `_runtime`
    与模块属性同一对象引用，直接改字段（而非替换对象），两边都可见。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.platform.scheduler as mod
    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    old_sf, old_iid = mod._runtime.session_factory, mod._runtime.instance_id
    mod._runtime.session_factory = session_factory
    mod._runtime.instance_id = "test-eval-nightly"
    yield session_factory
    mod._runtime.session_factory, mod._runtime.instance_id = old_sf, old_iid
    await engine.dispose()


@pytest.mark.asyncio
async def test_eval_nl2sql_mock_metrics(runtime):
    """NL2SQL 100 条 mock 回归：指标结构完整 + 报错率为 0（链路守护）。"""
    from app.platform.scheduler.jobs.eval_nightly import _eval_nl2sql

    metrics = await _eval_nl2sql("2026-09-02")
    assert "error" not in metrics, metrics
    assert metrics["count"] == 100
    for k in ("overall", "single", "join", "aggregation"):
        assert k in metrics, f"缺少 {k} 指标"
        assert 0.0 <= metrics[k] <= 1.0
    assert metrics["error_rate"] == 0.0, "mock 全链路不应有执行错误"
    assert metrics["exec_error"] == 0


@pytest.mark.asyncio
async def test_store_domain_idempotent(runtime):
    """落库幂等：(report_date, domain) 已存在 → 跳过；再次调用返回 skipped。"""
    from sqlalchemy import delete, select

    from app.platform.models import EvalReport
    from app.platform.scheduler.jobs.eval_nightly import _store_domain

    today = "2026-09-02"
    async with runtime() as s:
        await s.execute(
            delete(EvalReport).where(
                EvalReport.report_date == today, EvalReport.domain == "nl2sql"
            )
        )
        await s.commit()

    metrics = {"overall": 0.97, "single": 0.98, "join": 0.96, "aggregation": 1.0, "count": 100}
    try:
        await _store_domain(runtime, today, "nl2sql", metrics)
        async with runtime() as s:
            row = await s.scalar(
                select(EvalReport).where(
                    EvalReport.report_date == today, EvalReport.domain == "nl2sql"
                )
            )
            assert row is not None
            assert row.metrics["overall"] == 0.97
        dev2 = await _store_domain(runtime, today, "nl2sql", metrics)
        assert dev2 == {"skipped": True}, "同日期同域应幂等跳过"
    finally:
        async with runtime() as s:
            await s.execute(
                delete(EvalReport).where(
                    EvalReport.report_date == today, EvalReport.domain == "nl2sql"
                )
            )
            await s.commit()
