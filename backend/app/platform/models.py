"""平台库 ORM 模型（W23 Day2）——用户-角色-权限三级模型 + 伴随表。

设计要点（对应《02》3.1 节 DDL）：
- 五表：users / roles / permissions / role_permissions / user_roles
- 伴随表：audit_logs / approvals / feedback / api_keys + quota_usage /
  scheduler_job_runs / conversations
- SQLAlchemy 2.0 写法：`Mapped[T]` + `mapped_column()`（mypy 插件可识别）
- `password_hash VARCHAR(97)` 精确对应 bcrypt 输出长度（手册坑）
- `tenant_id` 多租户隔离键（W24 行级数据权限用）
- 毫秒时间戳用 MySQL 方言 `DATETIME(fsp=3)`：
  `sa.DateTime(3)` 的 3 被当成 timezone 参数（truthy），不会生成 `DATETIME(3)`，
  与 `CURRENT_TIMESTAMP(3)` 默认值不匹配 → 建表报 1067（手册未提，实测踩坑）
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """平台库声明式基类。"""


def _dt3() -> DATETIME:
    """返回 `DATETIME(3)` 类型（毫秒精度），兼容 `CURRENT_TIMESTAMP(3)` 默认值。"""
    return DATETIME(fsp=3)


# ==================== 五表：用户-角色-权限三级模型 ====================


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(97), nullable=False, comment="bcrypt")
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="多租户隔离键")
    status: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, comment="admin / operator / analyst / viewer"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        comment="kb:chat / ops:order:update / data:nl2sql / admin:user:manage",
    )
    domain: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="kb / ops / data / admin"
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)

    role_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    permission_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id"),)

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(Integer, primary_key=True)


# ==================== 伴随表：审计 / 审批 / 反馈 ====================


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_actor_created", "actor", "created_at"),
        Index("idx_audit_event_created", "event", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("200"))
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("idx_approval_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    approval_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    diff_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    diff_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(_dt3(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("idx_feedback_type_created", "fb_type", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fb_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="citation / sql")
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'open'"))
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


# ==================== 伴随表：SDK 机器身份与配额 ====================


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("idx_apikey_owner", "owner_user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="scm_ 前缀 + 前几位"
    )
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="sha256")
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class QuotaUsage(Base):
    __tablename__ = "quota_usage"
    __table_args__ = (
        UniqueConstraint("api_key_id", "period"),
        Index("idx_quota_period", "period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="Y-m-d 粒度（令牌桶配额记账）"
    )
    used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )


# ==================== 伴随表：调度 + 会话 ====================


class TokenBlacklist(Base):
    """JWT 吊销名单（W23 Day3 logout 落库版，W25 可迁 Redis）。

    设计要点：
    - 按 jti 精准吊销（不按整条 token 存，审计可回放哪个签名被吊销）
    - 记录过期时间：本周末 Redis 迁移前，吊销清理可直接 DELETE where expires_at < NOW()
    - 登出时把 access/refresh 的 jti 一并入库；校验时命中即 401
    """

    __tablename__ = "token_blacklist"
    __table_args__ = (
        Index("idx_blacklist_jti_expires", "jti", "expires_at"),
        Index("idx_blacklist_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="JWT ID")
    token_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="access / refresh")
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(_dt3(), nullable=False, comment="吊销截至")
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class SchedulerJobRun(Base):
    __tablename__ = "scheduler_job_runs"
    __table_args__ = (
        UniqueConstraint("job_id", "run_id"),
        Index("idx_jobrun_job_started", "job_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="防重幂等键")
    trigger: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(_dt3(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_dt3(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instance: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("idx_conv_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )
    est_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
