"""平台错误对齐（★ 与 backend `app/platform/errors.py` 的 Err 契约同源）。

平台所有 4xx/5xx 响应体统一 `{code, message, trace_id}`：
- `code`：机器可读错误码（见 `ErrorCode`，与后端常量一一对应）
- `message`：人类可读错误说明
- `trace_id`：贯穿一次请求的 request_id（排查定位用）

SDK 侧异常体系：
- `ScmError`：基类（携带 status_code / code / message / trace_id）
- `ScmAuthError`：401/403（凭证无效 / 权限不足）——可提示集成方检查 API Key
- `ScmQuotaError`：429（令牌桶限速超额）——携带 `retry_after`（秒），可退避重试
- `ScmServerError`：5xx（服务端/网关错误）——瞬时性较高，可指数退避重试
- 其余非 2xx 一律归一为 `ScmError`（据 status_code 兜底，据 code 精确分支）
"""

from __future__ import annotations


class ErrorCode:
    """平台错误码常量（与 backend `errors.ErrorCode` 对齐；单一事实来源）。"""

    BAD_REQUEST = "BAD_REQUEST_400"
    AUTH_UNAUTHORIZED = "AUTH_401"
    AUTH_FORBIDDEN = "AUTH_403"
    NOT_FOUND = "NOT_FOUND_404"
    VALIDATION = "VALIDATION_422"
    QUOTA_EXCEEDED = "QUOTA_429"
    INTERNAL = "INTERNAL_500"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE_503"


class ScmError(Exception):
    """平台 API 错误基类（对齐 Err 契约）。"""

    def __init__(self, status_code: int, code: str, message: str, trace_id: str = ""):
        super().__init__(f"[{code}] {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.trace_id = trace_id

    @classmethod
    def from_response(cls, response) -> ScmError:
        """从 httpx.Response 构造（解析 Err body；解析失败按状态码兜底）。"""
        try:
            body = response.json()
            code = str(body.get("code") or f"HTTP_{response.status_code}")
            message = str(body.get("message") or response.text[:500])
            trace_id = str(body.get("trace_id") or "")
        except Exception:  # noqa: BLE001  # 非 JSON 响应兜底文本
            code = f"HTTP_{response.status_code}"
            message = response.text[:500]
            trace_id = ""

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            return ScmQuotaError(
                response.status_code, code, message, trace_id,
                retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code in (401, 403):
            return ScmAuthError(response.status_code, code, message, trace_id)
        if response.status_code >= 500:
            return ScmServerError(response.status_code, code, message, trace_id)
        return cls(response.status_code, code, message, trace_id)


class ScmServerError(ScmError):
    """服务端/网关错误（5xx）：瞬时性较高，SDK 默认指数退避重试（W27 D4）。"""


class ScmAuthError(ScmError):
    """认证/授权失败（401 / 403）：检查 API Key 是否有效、权限是否覆盖。"""


class ScmQuotaError(ScmError):
    """配额超限（429）：令牌桶打满，按 `retry_after` 退避后重试。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        trace_id: str = "",
        retry_after: int | None = None,
    ):
        super().__init__(status_code, code, message, trace_id)
        self.retry_after = retry_after
