"""OpenTelemetry 接入（★ W22 Day3：Traces 支柱，三支柱打通）。

为什么（面试可讲）：
- W22 Day2 已落地 Logs（结构化日志）+ Metrics（Prometheus/Grafana）；Day3 补 Traces。
- OTEL 是业界标准：FastAPI 自动埋点（FastAPIInstrumentor）把"每条请求"变一条 trace，
  自动挂 request_id（自定义属性）→ 与 JSON 日志、Prometheus 三支柱通过 request_id/trace_id 关联。
- LLM 调用是 Agent 服务的核心外部依赖：手动 span 包住 LLM 调用（model / latency / tokens 属性），
  压测/真实排障时能看到"检索快、生成慢"的 trace 证据（面试第 48/44 题弹药）。

设计原则（对应手册坑）：
- 导出端可配：默认控制台（本地零依赖验证 trace 存在）；配 OTEL_EXPORTER=otlp +
  OTEL_OTLP_ENDPOINT 可导出到本地 collector / LangFuse（手册坑：别只打控制台，生产要配导出端）。
- fail-open：OTEL 未启用/导出异常绝不阻塞业务（观测旁路）。
- 三支柱关联：请求中间件拿 trace_id 写入 JSON 日志（日志同时有 request_id + trace_id），
  metrics 标签是 service，三者可通过时间窗 + request_id 对齐。

接口：
    setup(service_name, app)          # 初始化 TracerProvider + 导出器 + FastAPI 自动埋点（幂等）
    get_tracer(name="app") -> Tracer  # 业务手动 span 用（LLM 调用）
    trace_id() -> str | ""            # 当前 span 的 trace id（16 位 hex，无 span 返回 ""）
"""
import os

# ---- 开关：默认开（本地控制台导出零依赖；生产可关/改 OTLP）----
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "1") == "1"
OTEL_EXPORTER = os.getenv("OTEL_EXPORTER", "console")          # console | otlp
OTEL_OTLP_ENDPOINT = os.getenv("OTEL_OTLP_ENDPOINT", "")       # 如 http://localhost:4318/v1/traces

_tracer = None


def setup(service_name: str | None = None, app=None) -> None:
    """初始化 OTEL：TracerProvider + 导出器 + FastAPI 自动埋点（幂等，重复调用安全）。"""
    global _tracer
    if _tracer is not None:
        # 已初始化：若新 app 未埋点则补埋
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                FastAPIInstrumentor.instrument_app(app, tracer_provider=_tracer.provider)
            except Exception:
                pass
        return
    if not OTEL_ENABLED:
        _tracer = None
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        _svc: str = service_name or os.getenv("SERVICE_NAME", "app") or "app"
        resource = Resource.create({SERVICE_NAME: _svc})
        provider = TracerProvider(resource=resource)
        if OTEL_EXPORTER == "otlp" and OTEL_OTLP_ENDPOINT:
            exporter: object = OTLPSpanExporter(endpoint=OTEL_OTLP_ENDPOINT)
        else:
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))  # type: ignore[arg-type]
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(f"{service_name or 'app'}.obs")
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        print(f"[otel] 已启用（exporter={OTEL_EXPORTER}，service={service_name or 'app'}）")
    except Exception as e:
        print(f"[otel] 初始化失败（fail-open，不阻塞业务）: {type(e).__name__}: {str(e)[:100]}")
        _tracer = None


def get_tracer(name: str = "app"):
    """获取 tracer（未启用 → None，调用方需判空；fail-open）。"""
    if _tracer is None:
        return None
    try:
        return _tracer  # 单例 tracer 即可（span 名由调用方定）
    except Exception:
        return None


def trace_id() -> str:
    """当前 span 的 trace id（16 位 hex）。无 span / 未启用 → ""。"""
    if _tracer is None:
        return ""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return ""
        return format(ctx.trace_id, "x")[:16]
    except Exception:
        return ""
