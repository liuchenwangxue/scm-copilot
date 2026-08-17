"""JSON 结构化日志（★ W22 Day2：Logs 支柱）。

为什么（面试可讲）：
- 普通 print/text 日志只能人肉看，没法被 grep/检索/喂给日志平台（Loki/Splunk）。
- JSON lines（每行一个 JSON 对象）是日志平台的通用格式：可直接 grep、按字段过滤、
  聚合统计，也能被 promtail/Loki 直接采集。压测时"每条请求一条 JSON 日志"能精确对齐
  前端事件与后端处理，是排查线上问题的第一抓手（面试第 48 题：多轮执行出错定位）。

字段约定：
    ts         ISO8601 时间
    level      info / warning / error
    event      事件类型（http_request / chat_started / validator_fail / ...）
    request_id 一次请求的唯一 ID（由请求中间件注入，贯穿该请求所有日志）
    trace_id   OpenTelemetry trace id（W22 Day3 接入；未接入时为空串）
    method/path/status/duration_ms   HTTP 请求元数据（http_request 事件）
    ...        业务自定义字段（kv 展开）

设计原则（对应手册坑）：
- 一条请求 = 一条 http_request 日志（方法/路径/状态/耗时），压测可 grep 按状态/耗时聚合。
- 线程安全：多 worker / 并发请求写同一文件，用锁保护（单行 append 原子性兜底）。
- fail-open：写盘失败绝不抛异常（观测旁路，不阻塞业务）。
- 不记敏感内容：Key/PII 不入日志（与审计同原则）。

接口：
    setup(service_name, log_path=None) -> None          # 初始化根 handler（幂等）
    get_logger(name="app") -> logging.Logger            # 取业务 logger
    log_event(logger, event, level="info", **fields)    # 记一条结构化日志
    RequestLogMiddleware(app)                           # FastAPI 请求中间件（ASGI）
"""
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.shared import config

# ---- 默认结构化日志文件（项目 reports 目录）----
STRUCT_LOG = Path(config.REPORTS_DIR) / "struct.log.jsonl"

# ---- 模块级状态 ----
_handler: logging.Handler | None = None
_lock = threading.Lock()
_service_name = "app"


def _iso8601(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t or time.time()))


class JsonFormatter(logging.Formatter):
    """日志记录 → 单行 JSON（ts/level/event 固定，其余为 kv 展开的 fields）。"""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": _iso8601(record.created),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        # 业务 fields 从 msg 传入：支持 dict（推荐）或字符串
        if isinstance(record.msg, dict):
            data.update(record.msg)
        else:
            data["message"] = str(record.msg)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        # request_id / trace_id 从 record.__dict__ 透传（中间件/业务代码注入）
        for k in ("request_id", "trace_id"):
            v = getattr(record, k, None)
            if v:
                data[k] = v
        return json.dumps(data, ensure_ascii=False)


class _ThreadSafeFileHandler(logging.Handler):
    """带锁的 JSON lines 文件 handler（fail-open，写失败不抛）。"""

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self._flock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            # fail-open：观测失败不阻塞业务
            pass


def setup(service_name: str | None = None, log_path: str | Path | None = None) -> None:
    """初始化根日志 handler（幂等，多次调用只装一次）。"""
    global _handler, _service_name
    if service_name:
        _service_name = service_name
    if _handler is not None:
        return
    _handler = _ThreadSafeFileHandler(log_path or STRUCT_LOG)
    _handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 移除重复的旧 handler，避免重复写
    for h in list(root.handlers):
        if isinstance(h, _ThreadSafeFileHandler):
            root.removeHandler(h)
    root.addHandler(_handler)


def get_logger(name: str = "app") -> logging.Logger:
    """取业务 logger（先确保 setup 已调用；未调用时默认写到 reports/struct.log.jsonl）。"""
    setup()
    lg = logging.getLogger(f"{_service_name}.{name}")
    lg.propagate = True
    return lg


def log_event(logger: logging.Logger, event: str, level: str = "info",
              request_id: str = "", **fields: Any) -> None:
    """记一条结构化事件日志。event 必填，其余字段 kv 展开进 JSON。"""
    record: dict[str, Any] = {"event": event}
    record.update(fields)
    if request_id:
        record["request_id"] = request_id
    lvl = getattr(logging, level.upper(), logging.INFO)
    logger.log(lvl, record)


class RequestLogMiddleware:
    """ASGI 中间件：每条 HTTP 请求记一条 http_request 日志（方法/路径/状态/耗时）。

    同时为请求注入 request_id（X-Request-Id 响应头），贯穿该请求的后续业务日志，
    压测/前端事件可通过 request_id 精确对齐（面试第 48 题的多轮定位抓手）。
    """

    def __init__(self, app, service_name: str | None = None, enabled: bool | None = None):
        self.app = app
        self.logger = get_logger("http")
        if service_name:
            global _service_name
            _service_name = service_name
        # 默认开；可环境变量关闭（压测纯链路时不写日志）
        import os
        self.enabled = enabled if enabled is not None else (
            os.getenv("STRUCT_LOG_ENABLED", "1") == "1")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())[:12]
        t0 = time.time()
        method = scope.get("method", "")
        path = scope.get("path", "")

        # ★ W22 Day3：请求级 root span——保证每个请求必然有 trace_id（不依赖
        # FastAPIInstrumentor 的时序/机制），LLM span（real_provider）自动成为其子 span。
        # 未启用 OTEL → noop（trace_id() 返回 ""），fail-open。
        span = None
        span_ctx = None
        span_token = None
        try:
            from app.shared.obs import otel as _otel
            tracer = _otel.get_tracer()
            if tracer is not None:
                from opentelemetry import context as _ocontext
                from opentelemetry import trace as _otrace
                span = tracer.start_span(
                    "http.request",
                    attributes={"http.method": method, "http.path": path[:120],
                                "request_id": request_id})
                span_ctx = _otrace.set_span_in_context(span)
                span_token = _ocontext.attach(span_ctx)
        except Exception:
            span = None
            span_ctx = None
            span_token = None

        # 包装 send，捕获响应状态码
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 500)
                headers = list(message.get("headers", []))
                headers.append((b"X-Request-Id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_holder["code"] = 500
            raise
        finally:
            # 先取 trace_id（span 仍在 current 时），再 detach/end
            try:
                from app.shared.obs import otel as _otel
                tid = _otel.trace_id()
            except Exception:
                tid = ""
            if span is not None and span_token is not None:
                try:
                    from opentelemetry import context as _ocontext
                    span.set_attribute("http.status_code", status_holder["code"])
                    _ocontext.detach(span_token)
                    span.end()
                except Exception:
                    pass
            if self.enabled:
                duration_ms = (time.time() - t0) * 1000
                # trace_id 贯穿三支柱：日志同时有 request_id（业务）+ trace_id（OTEL）
                self.logger.info({
                    "event": "http_request",
                    "request_id": request_id,
                    "trace_id": tid,
                    "method": method,
                    "path": path[:120],
                    "status": status_holder["code"],
                    "duration_ms": round(duration_ms, 2),
                })


def read_logs(path: str | Path | None = None) -> list[dict]:
    """读取结构化日志（测试/报告用）。"""
    p = Path(path or STRUCT_LOG)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def clear_logs(path: str | Path | None = None) -> None:
    """清空结构化日志（测试用）。"""
    p = Path(path or STRUCT_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


if __name__ == "__main__":
    # 自检：setup + 记一条 + 读回验证 JSON 可解析
    clear_logs()
    setup()
    lg = get_logger("demo")
    log_event(lg, "demo_event", level="info", foo="bar", n=1)
    recs = read_logs()
    print(f"写入 {len(recs)} 条，首条: {recs[0] if recs else '(空)'}")
    assert recs and recs[0]["event"] == "demo_event" and recs[0]["foo"] == "bar"
    print("[logger] 自检通过：JSON 结构化日志可写可读可解析")
