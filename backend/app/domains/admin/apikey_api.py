"""API Key 管理 API（★ W25 Day5）：机器身份创建 / 列表 / 吊销。

权限：`admin:apikey:manage`（seed 新增，admin 角色全量拥有）。
- `POST /api/v1/admin/apikeys`：创建 Key（name + owner_username）→ 明文只返回一次；
  服务账号继承 owner 用户权限（机器身份语义）
- `GET /api/v1/admin/apikeys`：列表（仅前缀/名称/状态，不返回哈希与明文）
- `DELETE /api/v1/admin/apikeys/{key_id}`：吊销（enabled=0 软删除，保审计）

审计：创建/吊销各写 audit_logs（管理员操作留痕，与调度面板同模式）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from app.domains.admin.schemas import (
    ApiKeyCreatedOut,
    ApiKeyCreateIn,
    ApiKeyListOut,
    ApiKeyOut,
    ApiKeyRevokeOut,
)
from app.platform import apikeys, rbac
from app.platform.audit import write_audit
from app.platform.models import ApiKey, User

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post(
    "/apikeys",
    response_model=ApiKeyCreatedOut,
    summary="创建 API Key（机器身份）",
    description=(
        "生成 sk- 前缀机器身份 Key：sha256 哈希落库，明文只在创建时返回一次。"
        "Key 继承 owner_username 用户的全部权限（服务账号语义）。需要 admin:apikey:manage。"
    ),
)
async def create_api_key(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("admin:apikey:manage"))],
    body: ApiKeyCreateIn,
) -> ApiKeyCreatedOut:
    """创建 API Key：明文只返回一次（哈希落库 + 审计留痕）。"""
    owner_name = body.owner_username or current.username
    factory = request.app.state.session_factory
    async with factory() as session:
        owner = await session.scalar(
            select(User).where(User.username == owner_name)
        )
        if owner is None:
            raise HTTPException(status_code=404, detail=f"owner user not found: {owner_name}")

        row, plaintext = await apikeys.create_api_key(session, body.name, owner.id)
        await write_audit(
            session,
            event="admin.apikey.create",
            actor=current.username,
            target="/api/v1/admin/apikeys",
            detail={"key_id": row.id, "name": row.name, "owner": owner_name},
            trace_id=request.scope.get("request_id"),
        )
        await session.commit()
        return ApiKeyCreatedOut(
            key_id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            api_key=plaintext,
            owner_username=owner_name,
        )


@router.get(
    "/apikeys",
    response_model=ApiKeyListOut,
    summary="API Key 列表",
    description="列出全部 API Key（仅前缀/名称/状态，不返回哈希与明文）。需要 admin:apikey:manage。",
)
async def list_api_keys(
    request: Request,
    _: Annotated[User, Depends(rbac.require_permission("admin:apikey:manage"))],
) -> ApiKeyListOut:
    """列表：不暴露 key_hash（不可逆也应防扫描），明文创建时已一次性交付。"""
    factory = request.app.state.session_factory
    async with factory() as session:
        rows = list((await session.scalars(select(ApiKey).order_by(ApiKey.id.desc()))).all())
        owner_ids = {r.owner_user_id for r in rows if r.owner_user_id}
        names: dict[int, str] = {}
        if owner_ids:
            user_rows = list(
                (await session.execute(select(User.id, User.username).where(User.id.in_(owner_ids)))).all()
            )
            names = {uid: uname for uid, uname in user_rows}
    items = [
        ApiKeyOut(
            key_id=r.id,
            name=r.name,
            key_prefix=r.key_prefix,
            owner_username=names.get(r.owner_user_id) if r.owner_user_id else None,
            enabled=r.enabled == 1,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]
    return ApiKeyListOut(api_keys=items, total=len(items))


@router.delete(
    "/apikeys/{key_id}",
    response_model=ApiKeyRevokeOut,
    summary="吊销 API Key",
    description="吊销（enabled=0 软删除：哈希不可逆，保留记录保审计追溯）。需要 admin:apikey:manage。",
)
async def revoke_api_key(
    request: Request,
    key_id: int,
    current: Annotated[User, Depends(rbac.require_permission("admin:apikey:manage"))],
) -> ApiKeyRevokeOut:
    """吊销：软删除（已生成的 Key 立即失效；记录保留审计）。"""
    factory = request.app.state.session_factory
    async with factory() as session:
        row = await session.get(ApiKey, key_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"api key not found: {key_id}")
        row.enabled = 0
        await write_audit(
            session,
            event="admin.apikey.revoke",
            actor=current.username,
            target=f"/api/v1/admin/apikeys/{key_id}",
            detail={"key_id": key_id, "name": row.name, "key_prefix": row.key_prefix},
            trace_id=request.scope.get("request_id"),
        )
        await session.commit()
        return ApiKeyRevokeOut(ok=True, key_id=key_id, revoked=True)
