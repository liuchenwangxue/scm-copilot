"""real 提供方错误分类（★ W27 Day4 从 real_provider.py 拆出）。

纯函数模块（无 IO）——明天（D5）`test_real_provider_errors.py` 直接测这里：
- `_ProviderError`：带 HTTP 状态码的 Provider 错误
- `_retryable(exc)`：哪些错误值得重试（429/超时/5xx → True；400/401/403 → False）
- `_is_quota_error(exc)` / `_has_quota_kw(text)`：额度耗尽判定（命中即切模型）

依赖方向单向：被 model_pool / provider 引用，自身不 import 业务模块。
"""

from __future__ import annotations

import asyncio

import httpx

# 额度耗尽/权限类错误关键词（命中即切下一个模型）
_QUOTA_KEYWORDS = (
    "quota", "balance", "insufficient", "exhausted", "access denied",
    "rate limit", "free tier", "额度", "余额", "限流", "用量", "403",
    "max_tokens", "maximum context", "token limit", "额度不足", "欠费",
)


class _ProviderError(RuntimeError):
    """带 HTTP 状态码的 Provider 错误，用于错误分类。"""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def _retryable(exc: BaseException) -> bool:
    """哪些错误值得重试（W10 is_retryable 移植）：
    429 限流 / 超时 / 5xx 可重试；400/401/403 参数与权限不可重试。"""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, _ProviderError):
        if exc.status is None:
            return True
        return exc.status == 429 or exc.status >= 500
    text = str(exc)
    return any(k in text for k in ("timeout", "超时", "Connection", "连接", "503", "502", "504"))


def _has_quota_kw(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _QUOTA_KEYWORDS)


def _is_quota_error(exc: BaseException) -> bool:
    """是否为"模型额度耗尽"类错误（需要切模型，而非普通重试）。"""
    if isinstance(exc, _ProviderError):
        if exc.status == 429:
            return True
        if exc.status in (400, 403):
            return _has_quota_kw(exc.args[0] if exc.args else "")
    text = str(exc)
    return _has_quota_kw(text)
