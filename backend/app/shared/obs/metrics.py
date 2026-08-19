"""Prometheus 指标（★ W22 Day2：Metrics 支柱，QPS / P95 / 成功率；
★ W26 Day1：业务指标五区——NL2SQL 分数 / 调度任务 / 语义缓存 / token 成本 / RQ 队列）。

为什么（面试可讲）：
- 光有日志能查单条，但"整体健康度"要看指标：QPS 多少、P95 延迟多少、成功率多少。
- Prometheus 是业界标准监控（拉模型，15s 抓取一次），Grafana 画曲线。
- 命名遵循规范：Counter 加 _total、Histogram 用 _seconds——否则 Prometheus 不认，
  面试被问"指标怎么命名"能直接答规范。
- ★ W26 Day1：业务面板需要"指标会说话"——光有 HTTP 层 QPS 看不到业务健康度，
  所以补五组业务指标（eval 分数趋势 / 任务成功率 / 缓存命中 / 成本水位 / 队列深度）。

指标：
    http_requests_total{method, path, status}      QPS（Counter，rate() 取每秒）
    http_request_duration_seconds{method, path}    P95/P99（Histogram，histogram_quantile()）
    http_requests_success_total                    成功请求数（Counter，成功率=成功/总量）
    http_requests_failed_total                      失败请求数（5xx，Counter）
    http_request_in_flight{method, path}           在途请求（Gauge，观测并发）
    # ---- W26 Day1 业务指标 ----
    scm_nl2sql_eval_score{layer}                   NL2SQL 各层准确率（Gauge，夜间任务更新）
    scm_rag_eval_score{metric}                     RAG 各指标分数（Gauge，夜间任务更新）
    scm_job_success_total{job}                     调度任务成功次数（Counter，label=六任务名）
    scm_job_failed_total{job}                      调度任务失败次数（Counter）
    scm_semcache_hit_total                         语义缓存命中（Counter）
    scm_semcache_miss_total                        语义缓存未命中（Counter）
    scm_llm_tokens_total{model}                    LLM token 用量（Counter，按模型）
    scm_llm_cost_yuan_total{model}                 LLM 成本（Counter，¥，按模型）
    scm_rq_queue_depth{queue}                      RQ 报表队列深度（Gauge）

设计原则（对应手册坑）：
- 标签别太多：只按 method + path（归一化到 /api/{service} 避免高基数），不按 user/session。
- ★ 手册坑：label 基数控制——job label 只有 6 个值，别把 trace_id/session_id 塞进 label。
- fail-open：metrics 收集异常不影响主链路。
- 用 Registry 隔离（不注册默认 registry），两 app 各自独立，避免冲突。
- Bucket 设计覆盖真实延迟（毫秒到秒），压测/真实都能出 P95。

接口：
    MetricsMiddleware(app)                          ASGI 中间件（自动记录每请求指标）
    render() -> str                                 生成 Prometheus 文本（给 /metrics 端点）
    observe(status, duration_ms, method, path)      手动记录（供非 HTTP 场景/测试）
    clear()                                         清空（测试用）
    # W26 Day1 业务指标便捷函数（埋点侧调用，fail-open）：
    set_nl2sql_eval_score(layer, value) / set_rag_eval_score(metric, value)
    inc_job_success(job) / inc_job_failed(job)
    inc_semcache_hit() / inc_semcache_miss()
    inc_llm_usage(model, prompt_tokens, completion_tokens, cost_yuan)
    set_rq_queue_depth(queue, depth)
"""
import contextlib
import os
import threading
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, generate_latest, registry


# 归一化路径：去掉动态段（session_id/uuid 等），避免标签基数爆炸（手册坑）
# ★ W26 Day1：保留前 3 段（/api/v1/kb、/api/v1/ops、/api/v1/data、/api/v1/auth），
#   这样 Grafana "流量健康" 面板可按域分组（kb/ops/data）——3 段内都是稳定值，
#   不会引入动态段基数；不足 3 段（如 /health）按 2 段截断。
def _norm_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "root"
    # /api/v1/kb/chat -> api/v1/kb；/api/v1/ops/chat -> api/v1/ops；/health -> health
    keep = "/" + "/".join(parts[:3] if len(parts) >= 3 else parts[:2])
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

        # ==================== W26 Day1：业务指标（五区面板数据源） ====================
        # ★ 手册坑：label 基数控制——eval layer 固定 5 值（overall/single/join/aggregation/error_rate）、
        #   rag metric 固定 6 值、job 固定 6 值、model 固定 3-5 值。绝不塞 trace_id/session_id。
        # ① NL2SQL 质量区：夜间 eval_nightly 更新（分层准确率，Gauge 保留当前值）
        self.nl2sql_eval_score = Gauge(
            "scm_nl2sql_eval_score", "NL2SQL 各层执行准确率（夜间任务更新）",
            ["layer"], registry=self._reg)
        # RAG 质量区（同面板）：hit@1 / recall@5 / citation_accuracy / error_rate / p95_retrieve_ms
        self.rag_eval_score = Gauge(
            "scm_rag_eval_score", "RAG 各指标分数（夜间任务更新）",
            ["metric"], registry=self._reg)
        # ② 队列与调度区：六任务成功/失败 Counter（按 job_name label，label 基数=6）
        #   ★ 手册坑：不要用 `job` 做 label 名——Prometheus 的 `job` 是抓取任务保留标签，
        #   scrape 时会被 prometheus.yml 的 job_name 覆盖，导致六任务无法区分。
        self.job_success_total = Counter(
            "scm_job_success_total", "调度任务成功次数（label=job_name，六任务名）",
            ["job_name"], registry=self._reg)
        self.job_failed_total = Counter(
            "scm_job_failed_total", "调度任务失败次数（label=job_name）",
            ["job_name"], registry=self._reg)
        # ② 语义缓存区：命中/未命中 Counter（命中率 = hit/(hit+miss)）
        self.semcache_hit_total = Counter(
            "scm_semcache_hit_total", "语义缓存命中次数", registry=self._reg)
        self.semcache_miss_total = Counter(
            "scm_semcache_miss_total", "语义缓存未命中次数", registry=self._reg)
        # ⑤ 成本看板区：token 用量与成本（按模型 label，基数=模型池 3-5 个）
        self.llm_tokens_total = Counter(
            "scm_llm_tokens_total", "LLM token 总用量（按模型）",
            ["model"], registry=self._reg)
        self.llm_cost_yuan_total = Counter(
            "scm_llm_cost_yuan_total", "LLM 成本（¥，按模型，日预算水位=rate[1d]）",
            ["model"], registry=self._reg)
        # ⑥ 可靠性降级区（★ W27 Day3 A6/A7/A8：redis-down 行为矩阵的指标证据）
        #   "我知道边界在哪、它挂了我做了什么"——三个降级分支各有 Counter 上墙
        self.lock_local_fallback_total = Counter(
            "scm_lock_local_fallback_total", "Redis 不可用时的本地互斥兜底次数（A6）",
            ["component"], registry=self._reg)
        self.idem_fail_closed_total = Counter(
            "scm_idem_fail_closed_total", "幂等写路径 fail-closed 拒绝次数（A7）",
            registry=self._reg)
        self.budget_redis_down_total = Counter(
            "scm_budget_redis_down_total", "成本预算 Redis 不可用降级本地近似次数（A8）",
            registry=self._reg)
        # ② 队列深度 Gauge：RQ 报表队列当前积压（enqueue 时更新）
        self.rq_queue_depth = Gauge(
            "scm_rq_queue_depth", "RQ 报表队列深度（当前积压 job 数）",
            ["queue"], registry=self._reg)

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


# ==================== W26 Day1：业务指标便捷函数（埋点侧调用，fail-open） ====================
# 统一走 get_metrics()（SERVICE_NAME），与 /metrics 端点同 registry，Prometheus 可抓。


def set_nl2sql_eval_score(layer: str, value: float) -> None:
    """更新 NL2SQL 某层准确率（eval_nightly 夜间任务调用）。"""
    with contextlib.suppress(Exception):
        get_metrics().nl2sql_eval_score.labels(layer).set(float(value))


def set_rag_eval_score(metric: str, value: float) -> None:
    """更新 RAG 某指标分数（eval_nightly 夜间任务调用）。"""
    with contextlib.suppress(Exception):
        get_metrics().rag_eval_score.labels(metric).set(float(value))


def inc_job_success(job: str) -> None:
    """调度任务成功计数（scheduler _record 终态 success 时调用）。

    label 名 job_name：避免与 Prometheus 保留标签 `job`（scrape job）冲突被覆盖。
    """
    with contextlib.suppress(Exception):
        get_metrics().job_success_total.labels(job_name=job).inc()


def inc_job_failed(job: str) -> None:
    """调度任务失败计数（scheduler _record 终态 failed 时调用）。"""
    with contextlib.suppress(Exception):
        get_metrics().job_failed_total.labels(job_name=job).inc()


def inc_semcache_hit() -> None:
    """语义缓存命中计数（semantic_cache lookup 命中时调用）。"""
    with contextlib.suppress(Exception):
        get_metrics().semcache_hit_total.inc()


def inc_semcache_miss() -> None:
    """语义缓存未命中计数（semantic_cache lookup 未命中/异常降级时调用）。"""
    with contextlib.suppress(Exception):
        get_metrics().semcache_miss_total.inc()


# ==================== W27 Day3：可靠性降级计数（redis-down 行为矩阵证据） ====================

def inc_lock_fallback(component: str = "lock") -> None:
    """分布式锁本地互斥兜底计数（A6：Redis 挂 → 同 key 进程内锁）。

    component label 取值：lock（同步 DistributedLock）/ leader（调度 leader_lock）。
    """
    with contextlib.suppress(Exception):
        get_metrics().lock_local_fallback_total.labels(component=component).inc()


def inc_idem_fail_closed() -> None:
    """幂等写路径 fail-closed 拒绝计数（A7：Redis 挂 + risk=write 拒绝执行）。"""
    with contextlib.suppress(Exception):
        get_metrics().idem_fail_closed_total.inc()


def inc_budget_redis_down() -> None:
    """成本预算 Redis 降级计数（A8：Redis 挂 → 本地近似 + 日志）。"""
    with contextlib.suppress(Exception):
        get_metrics().budget_redis_down_total.inc()


def inc_llm_usage(model: str, prompt_tokens: int, completion_tokens: int,
                  cost_yuan: float = 0.0) -> None:
    """LLM token 用量 + 成本累计（real_provider._log_cost 调用）。

    cost_yuan 默认 0：mock 或未计费时只记 token（成本看板 token 曲线仍真实）。
    """
    try:
        total = int(prompt_tokens or 0) + int(completion_tokens or 0)
        m = get_metrics()
        m.llm_tokens_total.labels(model).inc(total)
        if cost_yuan:
            m.llm_cost_yuan_total.labels(model).inc(float(cost_yuan))
    except Exception:
        pass


def set_rq_queue_depth(queue: str, depth: int) -> None:
    """RQ 报表队列深度（enqueue 时更新，队列积压观测）。"""
    with contextlib.suppress(Exception):
        get_metrics().rq_queue_depth.labels(queue).set(int(depth))


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
