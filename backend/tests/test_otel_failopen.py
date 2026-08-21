"""★ W28-D6 otel.py 覆盖率收尾：连接失败路径 + 降级语义（B4/C9）。

覆盖（手册 Day6 第 3 条"补 otel.py 连接失败路径"）：
- OTEL_ENABLED=0 → 完全不初始化（get_tracer None / trace_id ""）
- setup 幂等：重复调用只初始化一次
- OTLP 导出器构造失败（exporter 导入/构造抛异常）→ fail-open，_tracer=None，不阻塞业务
- FastAPI 埋点失败 → 静默（app 已初始化时的补埋失败不抛）
- get_tracer 未启用 → None；trace_id 无 span → ""
- 启用后 get_tracer 可用；trace_id 在真实 span 内返回 16 位 hex

不依赖真实 OTLP 端点：连接失败用「导出器构造即抛」模拟（比真网络超时更快更稳）。
"""

import importlib

import pytest


def _reset() -> None:
    """还原 otel 模块状态（模块级 _tracer 单例跨测试污染）。"""
    import app.shared.obs.otel as otel

    otel._tracer = None
    otel.OTEL_ENABLED = True
    otel.OTEL_EXPORTER = "console"
    otel.OTEL_OTLP_ENDPOINT = ""


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    _reset()


def test_disabled_no_init():
    """OTEL_ENABLED=0 → 不初始化（get_tracer None / trace_id 空）。"""
    import app.shared.obs.otel as otel

    otel.OTEL_ENABLED = False
    otel.setup("svc-disabled")
    assert otel.get_tracer() is None
    assert otel.trace_id() == ""


def test_setup_console_idempotent(monkeypatch):
    """setup 幂等：重复调用只初始化一次（第二次不重建 tracer）。"""
    import app.shared.obs.otel as otel

    monkeypatch.setattr(otel, "OTEL_EXPORTER", "console")
    otel.setup("svc")
    first = otel.get_tracer()
    assert first is not None
    otel.setup("svc-2")  # 已初始化 → 幂等返回
    assert otel.get_tracer() is first  # 同一 tracer（未重建）


def test_setup_otlp_exporter_failure_fail_open(monkeypatch):
    """OTLP 导出器构造失败（端点不可达/导入缺包）→ fail-open，不阻塞业务。"""
    import app.shared.obs.otel as otel

    class _BoomExporter:
        def __init__(self, *a, **kw):
            raise ConnectionError("OTLP endpoint unreachable")

    monkeypatch.setattr(otel, "OTEL_ENABLED", True)
    monkeypatch.setattr(otel, "OTEL_EXPORTER", "otlp")
    monkeypatch.setattr(otel, "OTEL_OTLP_ENDPOINT", "http://127.0.0.1:1/v1/traces")
    # setup() 内部 `from opentelemetry.exporter... import OTLPSpanExporter`——
    # 必须 patch 源模块属性，patch otel.OTLPSpanExporter 无效（局部 import）
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as _otlp_mod

    monkeypatch.setattr(_otlp_mod, "OTLPSpanExporter", _BoomExporter)

    otel.setup("svc-otlp")
    assert otel.get_tracer() is None  # 失败 → 不提供 tracer（fail-open）
    assert otel.trace_id() == ""


def test_setup_instrument_failure_ignored(monkeypatch):
    """FastAPI 埋点失败 → 静默（已初始化后的补埋失败不抛）。"""
    import app.shared.obs.otel as otel

    monkeypatch.setattr(otel, "OTEL_ENABLED", True)
    otel.setup("svc")
    first = otel.get_tracer()
    assert first is not None

    def _boom_instrument(app, tracer_provider=None):
        raise RuntimeError("instrument failed")

    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        _boom_instrument,
        raising=False,
    )
    otel.setup("svc", app=object())  # 不应抛异常
    assert otel.get_tracer() is first


def test_setup_missing_package_fail_open(monkeypatch):
    """opentelemetry 包不可用（导入失败）→ fail-open 不阻塞业务。"""
    import sys

    import app.shared.obs.otel as otel

    # 模拟 import opentelemetry 失败
    real_import = __import__

    def _fake_import(name, *a, **kw):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("no opentelemetry installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(otel, "OTEL_ENABLED", True)
    monkeypatch.setattr("builtins.__import__", _fake_import)
    otel.setup("svc-no-otel")
    assert otel.get_tracer() is None
    assert otel.trace_id() == ""


def test_trace_id_within_span(monkeypatch):
    """真实 span 内 trace_id() 返回 16 位 hex。"""
    import app.shared.obs.otel as otel

    # 用 no-op exporter 替代 ConsoleSpanExporter——BatchSpanProcessor 后台线程
    # 在 pytest 捕获 stdout 关闭后 export 会抛"closed file"噪音（无害但脏输出）
    class _NoopExporter:
        def export(self, spans):
            return None

        def shutdown(self):
            return None

    import opentelemetry.sdk.trace.export as _exp

    monkeypatch.setattr(_exp, "ConsoleSpanExporter", _NoopExporter)
    monkeypatch.setattr(otel, "OTEL_ENABLED", True)
    monkeypatch.setattr(otel, "OTEL_EXPORTER", "console")
    otel.setup("svc-trace")
    tracer = otel.get_tracer()
    assert tracer is not None

    from opentelemetry import context as ocontext
    from opentelemetry import trace as otrace

    span = tracer.start_span("test")
    ctx = otrace.set_span_in_context(span)
    token = ocontext.attach(ctx)
    try:
        tid = otel.trace_id()
        assert tid and len(tid) <= 16 and all(c in "0123456789abcdef" for c in tid)
    finally:
        ocontext.detach(token)
        span.end()


def test_import_all_module_loads():
    """模块可正常 import（CI 安装齐全时不会挂）。"""
    m = importlib.import_module("app.shared.obs.otel")
    assert m is not None
