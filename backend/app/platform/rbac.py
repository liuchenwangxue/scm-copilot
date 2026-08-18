"""平台 RBAC（W23 Day3）——`require_permission(code)` 依赖注入工厂。

对应手册：`require_permission("ops:order:update")`——权限判定直接读 JWT claims 里的
`permissions` 列表，零查库（Day2 面试题"三级模型多一次 join 的规避"落地）。

依赖链（★ W25 Day5 双轨认证）：`api_key_or_jwt`（JWT 用户身份 或 API Key 机器身份
校验）→ 读 `current_permissions`（JWT 静态 claims / API Key 动态加载 owner 权限）→
命中返回 User；未命中抛 403。

用法：
    @router.get("/api/ops/orders")
    async def list_orders(_: User = Depends(require_permission("ops:order:read"))):
        ...
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.platform.apikeys import api_key_or_jwt
from app.platform.models import User


def require_permission(code: str) -> Callable:
    """返回一个 FastAPI 依赖，校验当前身份拥有指定权限码。

    ★ W25 Day5：认证入口升级为 `api_key_or_jwt`——JWT（用户身份）与
    API Key（机器身份，SDK/集成方）双轨都经此判定权限；API Key 的权限
    由 `authenticate_api_key` 动态加载 owner 用户权限塞进 `_jwt_permissions`，
    与 JWT 静态 claims 走同一套 `current_permissions` 判定。
    """

    async def dependency(
        current: Annotated[User, Depends(api_key_or_jwt)],
    ) -> User:
        perms = current_permissions(current)
        if code not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"permission required: {code}",
            )
        return current

    return dependency


def current_permissions(current: User) -> set[str]:
    """从当前请求的 JWT claims 取权限集。

    `get_current_user` 校验通过后，我们把 claims 暂存到 `current.__dict__`（轻量缓存），
    这里读回；避免为了拿权限再查一次库。若无权限 claims（如 refresh 场景），返回空集。
    """
    claims = getattr(current, "_jwt_permissions", None)
    return set(claims or [])


def require_any_of(*codes: str) -> Callable:
    """任一权限即可（如 admin 端点常放行 `admin:*` + 该域权限）。"""

    async def dependency(
        current: Annotated[User, Depends(api_key_or_jwt)],
    ) -> User:
        perms = current_permissions(current)
        if not perms.intersection(codes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"permission required: any of {codes}",
            )
        return current

    return dependency
