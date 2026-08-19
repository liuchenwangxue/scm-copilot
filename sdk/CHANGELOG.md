# Changelog

## 0.2.0（2026-08-23）★ W27 Day4：自动退避重试

### Added（行为变化——默认开启）
- **429 自动退避重试**：收到 `QUOTA_429` 时尊重服务端 `Retry-After` 头（≤30s 才重试，
  无/超 30s 立即抛），与平台侧令牌桶形成闭环协议——调用方无需再手写退避逻辑。
- **5xx / 超时自动重试**：`ScmServerError`（5xx）与 `httpx.TimeoutException`
  指数退避 + 抖动重试（`min(2^n + jitter, 8)`），应对网关瞬时错误。
- **`ScmServerError` 异常类型**：5xx 独立归类（此前归一为 `ScmError`），
  便于调用方按"可重试的服务端错误"精确分支。

### Changed
- `ScmCopilot(...)` 新增参数：`auto_retry=True`（默认开）、`max_retries=2`——
  非幂等/自定义请求可传 `auto_retry=False` 关闭。
- `__version__` 0.1.0 → 0.2.0。

### Behavior notes
- 非 429 的 4xx（认证/校验）**不重试**——重试也不会变好。
- 只对幂等请求重试（查询 / 带 `approval_id` 幂等键的审批决策）；
  调用方自定义非幂等写请求时应关闭 `auto_retry`。

## 0.1.0（2026-08-21）
- 首版三接口：`chat_stream`（SSE）/ `nl2sql`（含 pandas）/ `approvals`。
- Err 契约异常体系：`ScmError` / `ScmAuthError` / `ScmQuotaError`。
- 认证：`api_key="sk-..."`（机器身份）或 `token="<JWT>"`（用户身份）。
