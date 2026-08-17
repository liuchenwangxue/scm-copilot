"""观测（Obs）子包（★ W22 Day2：可观测性两大支柱 Logs + Metrics）。

子模块：
    logger.py   JSON 结构化日志 + FastAPI 请求中间件（Logs 支柱）
    metrics.py  Prometheus 指标 + /metrics 端点（Metrics 支柱）

设计原则（面试可讲）：
- 三支柱（Logs / Metrics / Traces）中，本包先落地 Logs + Metrics；
  Traces（OpenTelemetry）在 W22 Day3 接入，届时 request_id/trace_id 贯穿三支柱。
- 观测是旁路：日志/指标写失败绝不影响主链路（fail-open），观测本身开销可忽略。
- Metrics 命名遵循 Prometheus 规范（Counter 加 _total / Histogram 加 _seconds），标签少防基数爆炸。
"""
