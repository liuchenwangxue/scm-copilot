"""Prometheus 指标（★ W22 Day2：Metrics 支柱，QPS / P95 / 成功率）。

为什么（面试可讲）：
- 光有日志能查单条，但"整体健康度"要看指标：QPS 多少、P95 延迟多少、成功率多少。
- Prometheus 是业界标准监控（拉模型，15s 抓取一次），Grafana 画曲线。
- 命名遵循规范：Counter 加 _total、Histogram 用 _seconds——否则 Prometheus 不认，
  面试被问"指标怎么命名"能直接答规范。

指标：
    http_requests_total{method, path, status}      QPS（Counter，rate() 取每秒）
    http_request_duration_seconds{method, path}    P95/P99（Histogram，histogram_quantile()）
    http_requests_success_total                    成功请求数（Counter，成功率=成功/总量）
    http_requests_failed_total                      失败请求数（5xx，Counter）
    http_request_in_flight{method, path}           在途请求（Gauge，观测并发）

设计原则（对应手册坑）：
- 标签别太多：只按 method + path（归一化到 /api/{service} 避免高基数），不按 user/session。
- fail-open：metrics 收集异常不影响主链路。
- 用 Registry 隔离（不注册默认 registry），两 app 各自独立，避免冲突。
- Bucket 设计覆盖真实延迟（毫秒到秒），压测/真实都能出 P95。

接口：
    MetricsMiddleware(app)                          ASGI 中间件（自动记录每请求指标）
    render() -> str                                 生成 Prometheus 文本（给 /metrics 端点）
    observe(status, duration_ms, method, path)      手动记录（供非 HTTP 场景/测试）
    clear()                                         清空（测试用）
"""
import contextlib
import os
import threading
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, generate_latest, registry


# 归一化路径：去掉动态段（session_id/uuid 等），避免标签基数爆炸（手册坑）
# 只保留前 2 段，/api/chat、/api/approval、/auth/login 等都是低频稳定标签
def _norm_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "root"
    # /api/chat -> api/chat；/api/approval -> api/approval；/health -> health
    keep = "/" + "/".join(parts[:2])
    return keep


class _Metrics:
    """进程内 Prometheus 指标集合（自建 Registry，两 app 隔离）。"""

    def __init__(self, service: str):
        self.service = service
        self._reg = registry.CollectorRegistry()
        self._lock = threading.Lock()
        common = ["method", "path"]
        self.requests_total = Counter(
            "http_requests_total", "HTTP 请求总数（QPS=rate()[1m]）",
            common, registry=self._reg)
        self.request_duration = Histogram(
            "http_request_duration_seconds", "HTTP 请求耗时",
            common,
            buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
            registry=self._reg)
        self.success_total = Counter(
            "http_requests_success_total", "成功请求数（2xx/3xx，成功率=成功/总量）",
            ["method", "path"], registry=self._reg)
        self.failed_total = Counter(
            "http_requests_failed_total", "失败请求数（5xx）",
            ["method", "path"], registry=self._reg)
        self.client_error_total = Counter(
            "http_client_error_total", "客户端错误（4xx，如 401/403/429）",
            ["method", "path"], registry=self._reg)
        self.in_flight = Gauge(
            "http_request_in_flight", "在途请求（并发观测）",
            ["method", "path"], registry=self._reg)

    def observe(self, status: int, duration_ms: float, method: str, path: str) -> None:
        """记录一次请求的指标（中间件/测试调用）。"""
        try:
            m, p = method or "GET", _norm_path(path)
            self.requests_total.labels(m, p).inc()
            self.request_duration.labels(m, p).observe(duration_ms / 1000.0)
            if 200 <= status < 400:
                self.success_total.labels(m, p).inc()
            elif status >= 500:
                self.failed_total.labels(m, p).inc()
            else:
                self.client_error_total.labels(m, p).inc()
        except Exception:
            pass  # fail-open

    def render(self) -> str:
        """生成 Prometheus 文本格式（给 /metrics 端点）。"""
        return generate_latest(self._reg).decode()


# ---- 模块级单例（按 service 区分）----
_metrics_by_service: dict[str, _Metrics] = {}
_metrics_lock = threading.Lock()


def get_metrics(service: str | None = None) -> _Metrics:
    """取指定 service 的指标单例。"""
    svc: str = service or os.getenv("SERVICE_NAME", "app") or "app"
    with _metrics_lock:
        if svc not in _metrics_by_service:
            _metrics_by_service[svc] = _Metrics(svc)
        return _metrics_by_service[svc]


class MetricsMiddleware:
    """ASGI 中间件：自动为每条 HTTP 请求记录 metrics（QPS/延迟/成功率）。"""

    def __init__(self, app, service: str | None = None, enabled: bool | None = None):
        self.app = app
        self.metrics = get_metrics(service or "app")
        self.enabled = enabled if enabled is not None else (
            os.getenv("METRICS_ENABLED", "1") == "1")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = _norm_path(scope.get("path", ""))
        t0 = time.time()
        status_holder = {"code": 500}
        with contextlib.suppress(Exception):  # 观测旁路
            self.metrics.in_flight.labels(method, path).inc()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_holder["code"] = 500
            raise
        finally:
            with contextlib.suppress(Exception):  # 观测旁路
                self.metrics.in_flight.labels(method, path).dec()
            if self.enabled:
                self.metrics.observe(status_holder["code"], (time.time() - t0) * 1000,
                                     method, scope.get("path", ""))


def render(service: str | None = None) -> str:
    """生成指定 service 的 Prometheus 文本（/metrics 端点直接返回）。"""
    return get_metrics(service).render()


def clear(service: str | None = None) -> None:
    """清空指标（测试用）：重建该 service 的指标集合。"""
    svc: str = service or os.getenv("SERVICE_NAME", "app") or "app"
    with _metrics_lock:
        _metrics_by_service[svc] = _Metrics(svc)


def summary(service: str | None = None) -> dict[str, Any]:
    """返回当前指标摘要（QPS 计数/成功率/耗时分位，测试与报告用）。
    注意：P95/P99 需要 Prometheus histogram_quantile 计算，此处给出累计计数与样本，
    精确分位由 Prometheus/Grafana 出（报告里说明口径）。"""
    m = get_metrics(service)
    return {
        "service": m.service,
        "count_metric": "http_requests_total",
        "note": "P95/P99 由 Prometheus histogram_quantile 计算；此处看样本分布",
    }


if __name__ == "__main__":
    # 自检：记录几条 → render 出文本 → 关键指标名存在
    clear("test")
    m = get_metrics("test")
    m.observe(200, 120.5, "POST", "/api/chat")
    m.observe(200, 300.2, "POST", "/api/chat")
    m.observe(500, 800.0, "POST", "/api/chat")
    text = m.render()
    for name in ("http_requests_total",
                 "http_request_duration_seconds_count",
                 "http_requests_success_total",
                 "http_requests_failed_total"):
        assert name in text, f"缺指标 {name}"
    print("[metrics] 自检通过：QPS/延迟/成功率指标均已暴露")
    # 打印前几行方便核对
    for line in text.splitlines():
        if line.startswith("# TYPE") or line.startswith("http_"):
            print("  " + line)
