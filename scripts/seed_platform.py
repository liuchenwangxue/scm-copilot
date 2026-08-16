"""平台库幂等种子脚本（W23 Day2）。

填充内容（固定 seed，连跑两遍结果一致）：
- 4 角色：admin / operator / analyst / viewer（固定 id 1–4）
- 12 权限码：kb 3 + ops 4 + data 2 + admin 3
- 角色-权限映射：admin 全量 / operator(kb+ops) / analyst(kb+data) / viewer(kb 只读)
- 3 租户 × 4 角色测试用户（密码明文 `Passw0rd!`，bcrypt 入库，写进 README 开发文档）

幂等实现：先查后插（`SELECT id` 不存在才 INSERT），对 roles/permissions 用
固定 id 保证引用稳定；user_roles/role_permissions 复合主键 ON DUPLICATE KEY 兜底。
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# 脚本从项目根目录跑：把 backend 加入 import path（pyproject pythonpath 只在 pytest 生效）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.platform.models import (
    Base,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.platform.settings import settings

# ---- 固定 seed 定义 ----

# 角色（固定 id 保证 user_roles 引用稳定）
ROLES: list[dict[str, Any]] = [
    {"id": 1, "code": "admin", "name": "管理员"},
    {"id": 2, "code": "operator", "name": "运营专员"},
    {"id": 3, "code": "analyst", "name": "数据分析师"},
    {"id": 4, "code": "viewer", "name": "只读访客"},
]

# 12 条权限码（kb 3 + ops 4 + data 2 + admin 3）
PERMISSIONS: list[dict[str, Any]] = [
    # kb 域（知识问答）
    {"code": "kb:chat", "domain": "kb", "name": "知识问答对话"},
    {"code": "kb:read", "domain": "kb", "name": "知识库检索"},
    {"code": "kb:feedback", "domain": "kb", "name": "引用纠错反馈"},
    # ops 域（业务操作）
    {"code": "ops:order:read", "domain": "ops", "name": "订单查询"},
    {"code": "ops:order:update", "domain": "ops", "name": "订单修改（高危走审批）"},
    {"code": "ops:approval:manage", "domain": "ops", "name": "审批处理"},
    {"code": "ops:tool:execute", "domain": "ops", "name": "业务工具执行"},
    # data 域（数据分析 / NL2SQL）
    {"code": "data:nl2sql", "domain": "data", "name": "自然语言查数据"},
    {"code": "data:feedback", "domain": "data", "name": "SQL 纠错反馈"},
    # admin 域（平台管理）
    {"code": "admin:user:manage", "domain": "admin", "name": "用户角色管理"},
    {"code": "admin:audit:read", "domain": "admin", "name": "审计日志查看"},
    {"code": "admin:scheduler:manage", "domain": "admin", "name": "调度任务管理"},
]

# 角色 → 权限码集合
ROLE_PERMISSION_MAP: dict[str, set[str]] = {
    "admin": {p["code"] for p in PERMISSIONS},  # 全量
    "operator": {
        "kb:chat",
        "kb:read",
        "kb:feedback",
        "ops:order:read",
        "ops:order:update",
        "ops:approval:manage",
        "ops:tool:execute",
    },
    "analyst": {"kb:chat", "kb:read", "data:nl2sql", "data:feedback"},
    "viewer": {"kb:chat", "kb:read"},
}

# 测试密码明文（bcrypt 入库）
PLAIN_PASSWORD = "Passw0rd!"

# 3 租户 × 4 角色
TENANTS = ["t_huadong", "t_huabei", "t_huanan"]


def _build_users() -> list[dict[str, Any]]:
    pwd_hash = bcrypt.hashpw(PLAIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
    users: list[dict[str, Any]] = []
    for tenant in TENANTS:
        for role in ROLES:
            users.append(
                {
                    "username": f"{role['code']}_{tenant}",
                    "password_hash": pwd_hash,
                    "tenant_id": tenant,
                    "role_code": role["code"],
                }
            )
    return users


async def _seed_roles(session_factory) -> None:
    async with session_factory() as session:
        for r in ROLES:
            exists = await session.get(Role, r["id"])
            if exists is None:
                session.add(Role(id=r["id"], code=r["code"], name=r["name"]))
        await session.commit()


async def _seed_permissions(session_factory) -> None:
    async with session_factory() as session:
        for p in PERMISSIONS:
            exists = await session.scalar(
                text("SELECT id FROM permissions WHERE code = :code"),
                {"code": p["code"]},
            )
            if exists is None:
                session.add(Permission(code=p["code"], domain=p["domain"]))
        await session.commit()


async def _seed_role_permissions(session_factory) -> None:
    async with session_factory() as session:
        # 先取 code→id 映射
        role_rows = await session.execute(text("SELECT id, code FROM roles"))
        role_id = {code: rid for rid, code in role_rows.all()}
        perm_rows = await session.execute(text("SELECT id, code FROM permissions"))
        perm_id = {code: pid for pid, code in perm_rows.all()}
        for role_code, perm_codes in ROLE_PERMISSION_MAP.items():
            for pc in perm_codes:
                exists = await session.scalar(
                    text(
                        "SELECT 1 FROM role_permissions "
                        "WHERE role_id = :rid AND permission_id = :pid"
                    ),
                    {"rid": role_id[role_code], "pid": perm_id[pc]},
                )
                if exists is None:
                    await session.execute(
                        text(
                            "INSERT INTO role_permissions (role_id, permission_id) "
                            "VALUES (:rid, :pid)"
                        ),
                        {"rid": role_id[role_code], "pid": perm_id[pc]},
                    )
        await session.commit()


async def _seed_users(session_factory) -> None:
    users = _build_users()
    async with session_factory() as session:
        for u in users:
            exists = await session.scalar(
                text("SELECT id FROM users WHERE username = :username"),
                {"username": u["username"]},
            )
            if exists is None:
                user = User(
                    username=u["username"],
                    password_hash=u["password_hash"],
                    tenant_id=u["tenant_id"],
                )
                session.add(user)
                await session.flush()  # 取 id 关联 user_roles
                role_id = {"admin": 1, "operator": 2, "analyst": 3, "viewer": 4}[u["role_code"]]
                session.add(UserRole(user_id=user.id, role_id=role_id))
        await session.commit()


async def seed_all(dsn: str) -> None:
    engine = create_async_engine(dsn, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # 确保表存在（幂等：alembic 已建则跳过）
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
        await _seed_roles(session_factory)
        await _seed_permissions(session_factory)
        await _seed_role_permissions(session_factory)
        await _seed_users(session_factory)
        await _print_summary(engine)
    finally:
        await engine.dispose()


async def _print_summary(engine) -> None:
    async with engine.connect() as conn:
        for table in ("roles", "permissions", "role_permissions", "user_roles", "users"):
            cnt = await conn.scalar(text(f"SELECT COUNT(*) FROM {table}"))
            print(f"  {table}: {cnt}")


async def main() -> None:
    dsn = settings.platform_dsn
    print(f"Seeding platform tables @ {dsn}")
    await seed_all(dsn)
    print("Seed 完成（幂等，连跑两遍结果一致）")


if __name__ == "__main__":
    asyncio.run(main())
