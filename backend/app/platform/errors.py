"""统一错误响应契约（★ W25 Day4 OpenAPI 规范化）。

目标：所有 4xx/5xx 响应体统一 `{code, message, trace_id}`（SDK 侧对齐同一套 code）：
- `code`：机器可读错误码（集中定义本模块，避免散落字符串——手册 Day4 坑）
- `message`：人类可读错误说明（承载原 HTTPException.detail）
- `trace_id`：贯穿一次请求的 request_id（RequestIdMiddleware 写入 scope）

用法：
    from app.platform.errors import Err, ErrorCode, register_error_handlers
    app = FastAPI(responses={401: {"model": Err}, ...})  # OpenAPI 声明
    register_error_handlers(app)                          # 运行时统一格式化

设计权衡：
- HTTPException.detail 已覆盖全部既有 4xx 语义；这里只做"格式归一"，业务逻辑不动
- 500 统一 INTERNAL_500（不向客户端泄露内部堆栈；服务端 logger 留全量堆栈）
- 422 校验错误把 FastAPI 的 errors 数组 JSON 序列化进 message（保留字段级详情）
- 错误码命名 `<域>_<http状态>`：SDK 可据 status_code 兜底，据 code 精确分支
"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("scm.platform.errors")


class ErrorCode:
    """错误码常量（单一事实来源；SDK 侧 `scm_client.errors` 对齐同一套）。

    `message` 字段承载具体原因（如 "invalid credentials"），
    `code` 用于机器分支（重试策略 / 前端文案映射）。
    """

    BAD_REQUEST = "BAD_REQUEST_400"  # 参数语义错误（业务端点显式 400）
    AUTH_UNAUTHORIZED = "AUTH_401"  # 未认证 / 凭证无效 / 过期 / 已吊销
    AUTH_FORBIDDEN = "AUTH_403"  # 已认证但权限码未命中
    NOT_FOUND = "NOT_FOUND_404"  # 资源不存在
    VALIDATION = "VALIDATION_422"  # 请求体校验失败
    QUOTA_EXCEEDED = "QUOTA_429"  # 限流 / API Key 配额超限
    INTERNAL = "INTERNAL_500"  # 服务内部错误（不泄露堆栈）
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE_503"  # 依赖服务不可用（如调度器）


class Err(BaseModel):
    """统一错误响应体（OpenAPI `responses={401: {"model": Err}, ...}` 引用）。"""

    code: str = Field(description="机器可读错误码，如 AUTH_401 / QUOTA_429")
    message: str = Field(description="人类可读错误说明")
    trace_id: str = Field(description="贯穿一次请求的 request_id（排查定位用）")


# HTTP 状态 → 错误码映射（未显式声明的状态回退 BAD_REQUEST/INTERNAL）
_STATUS_TO_CODE: dict[int, str] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.AUTH_UNAUTHORIZED,
    403: ErrorCode.AUTH_FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    422: ErrorCode.VALIDATION,
    429: ErrorCode.QUOTA_EXCEEDED,
    500: ErrorCode.INTERNAL,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def _err_payload(request: Request, code: str, message: str) -> dict[str, str]:
    """组装 Err 响应体；trace_id 取 RequestIdMiddleware 写入的 scope（缺省空串）。"""
    return {
        "code": code,
        "message": message,
        "trace_id": request.scope.get("request_id") or "",
    }


def _message_from_detail(detail: object) -> str:
    """detail 可能是 str（常规）/ dict / list（422 errors）——统一序列化为字符串。"""
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """HTTPException → 统一 Err 格式（业务抛出的 4xx 全部经此归一）。"""
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.BAD_REQUEST)
    return JSONResponse(
        status_code=exc.status_code,
        content=_err_payload(request, code, _message_from_detail(exc.detail)),
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI 请求体校验失败 → VALIDATION_422（message 保留字段级 errors 详情）。"""
    return JSONResponse(
        status_code=422,
        content=_err_payload(
            request, ErrorCode.VALIDATION, _message_from_detail(exc.errors())
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 500：服务端留全量堆栈，客户端只见 INTERNAL_500（不泄露内部信息）。"""
    logger.exception(
        "unhandled error on %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content=_err_payload(request, ErrorCode.INTERNAL, "internal server error"),
    )


def register_error_handlers(app) -> None:
    """注册三个全局异常处理器（main.py 启动时调用一次）。"""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
