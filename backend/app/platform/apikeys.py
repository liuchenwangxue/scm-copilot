"""★ W25 Day5：API Key 机器身份 + 令牌桶限速（开放生态的准入闸）。

机器身份 vs 用户身份（与 JWT 并存）：
- JWT：用户身份——15min 短命、交互式前端用、权限静态快照在 claims
- API Key：机器身份——长期有效、SDK/集成方用、`sk-` 前缀 + secrets 生成、
  明文只在创建时返回一次（之后只能看到前缀），哈希落库

设计权衡（手册 Day5 坑逐条落实）：
- Key 哈希用 **sha256** 而非 bcrypt：Key 校验是每请求高频路径，bcrypt 100ms 太慢；
  与密码 bcrypt 策略分开（密码低频 + 防撞库，Key 高频 + 128bit 熵足够高防猜测）
- 认证双轨 `api_key_or_jwt`：Bearer 以 `sk-` 开头 → API Key 认证（动态查库加载
  owner 用户权限），否则走 JWT（静态 claims）——集成方无登录态也能调受保护端点
- 令牌桶限速：Redis Lua **原子**执行（容量 10 / 速率 5/min），超额 429 + Retry-After；
  Redis 不可用 → fail-open 放行（配额是软约束，宁可多跑不可卡死）
- 吊销：enabled=0 软删除（哈希不可逆，物理删除会破坏审计可追溯性）
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform.auth import (
    _load_user_permissions,
    bearer_scheme,
    get_current_user,
    get_session,
)
from app.platform.models import ApiKey, User
from app.platform.settings import settings
from app.shared.reliability.redis_client import RedisClient, get_redis_client

API_KEY_PREFIX = "sk-"

# ---- 令牌桶参数（手册：容量 10 / 速率 5/min 起）----
TOKEN_BUCKET_CAPACITY = 10
TOKEN_BUCKET_REFILL_PER_MIN = 5
# 纯逻辑用：refill 换算成每秒补充速率
_REFILL_PER_SEC = TOKEN_BUCKET_REFILL_PER_MIN / 60.0

# Redis key 前缀（与锁/幂等区分命名空间，scan 清理可定向）
_BUCKET_KEY = "rate:key:{key_hash}"
_BUCKET_TS_KEY = "rate:key:{key_hash}:ts"


# ==================== Key 生成 / 哈希 ====================


def generate_api_key() -> str:
    """生成机器身份 Key：`sk-` + 48 位十六进制（192bit 熵，防猜测）。

    明文只在 create 时返回一次——之后所有存储/比对都用 sha256 哈希。
    """
    return f"{API_KEY_PREFIX}{secrets.token_hex(24)}"


def hash_api_key(key: str) -> str:
    """sha256 哈希（Key 校验每请求高频路径，bcrypt 100ms 太慢——面试可讲权衡）。"""
    return hashlib.sha256(key.encode()).hexdigest()


def key_prefix_of(key: str) -> str:
    """展示用前缀：`sk-` + 前 8 位（api_keys.key_prefix 字段，VARCHAR(16)）。"""
    body = key[len(API_KEY_PREFIX) :] if key.startswith(API_KEY_PREFIX) else key
    return f"{API_KEY_PREFIX}{body[:8]}"


# ==================== Key 生命周期（库操作） ====================


async def create_api_key(
    session: AsyncSession, name: str, owner_user_id: int
) -> tuple[ApiKey, str]:
    """创建 Key：生成明文 → 哈希落库 → 返回 (model, 明文)。

    明文只在创建时返回一次；后续查询只展示 key_prefix。
    """
    key = generate_api_key()
    row = ApiKey(
        key_prefix=key_prefix_of(key),
        key_hash=hash_api_key(key),
        name=name,
        owner_user_id=owner_user_id,
        enabled=1,
    )
    session.add(row)
    await session.flush()
    return row, key


async def revoke_api_key(session: AsyncSession, key_id: int) -> bool:
    """吊销 Key（enabled=0 软删除——哈希不可逆，保留记录保审计）。"""
    row = await session.get(ApiKey, key_id)
    if row is None:
        return False
    row.enabled = 0
    return True


async def authenticate_credentials(
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None,
) -> User:
    """统一身份解析（★ W27-D6 B11：sk- 检测单处实现）。

    被 `main.py` 全局门禁（只认证不限速）与端点级 `api_key_or_jwt`（认证+限速）
    共用——Bearer `sk-` 前缀判定与"无效 Key → 401"逻辑不再两处维护：

    - Bearer `sk-` → API Key 认证（sha256 查表，无效抛 401）；
    - 否则 → JWT 校验（`get_current_user` 内部处理 401/吊销/过期）。
    """
    if credentials is not None and credentials.credentials.startswith(API_KEY_PREFIX):
        user = await authenticate_api_key(session, credentials.credentials)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
            )
        return user
    return await get_current_user(credentials=credentials, session=session)


async def authenticate_api_key(session: AsyncSession, key: str) -> User | None:
    """API Key 认证：sha256 查表 → enabled 校验 → owner 用户存活 → 返回 User。

    权限动态加载（服务账号继承 owner 用户权限），塞 `_jwt_permissions`——
    与 JWT 的静态 claims 权限走同一套 `rbac.current_permissions` 判定，零查库差异。
    无效 Key / 已吊销 / owner 被禁用 → None（调用方抛 401，语义与 JWT 一致）。
    """
    if not key.startswith(API_KEY_PREFIX):
        return None
    row = await session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))
    )
    if row is None or row.enabled != 1:
        return None
    user = await session.get(User, row.owner_user_id) if row.owner_user_id else None
    if user is None or user.status != 1:
        return None
    # 动态加载 owner 用户当前权限（API Key 是长期凭证，权限变更实时生效——
    # 与 JWT 静态快照 15min 过期不同，这是机器身份的语义差异，可面试讲）
    perms = await _load_user_permissions(session, user.id)
    setattr(user, "_jwt_permissions", perms)  # noqa: B010
    return user


# ==================== 令牌桶限速 ====================


def token_bucket_allow(
    state: tuple[float, float],
    now: float,
    capacity: float = TOKEN_BUCKET_CAPACITY,
    refill_per_sec: float = _REFILL_PER_SEC,
) -> tuple[bool, tuple[float, float], int]:
    """纯逻辑令牌桶（★ 可单测：不依赖 Redis 的行为推演）。

    Args:
        state: (tokens, last_ts)——上次更新后的令牌数与时间戳
        now: 当前时间（秒）
        capacity: 桶容量（最大令牌数）
        refill_per_sec: 每秒补充速率

    Returns:
        (allowed, new_state, retry_after_seconds)
    """
    tokens, last_ts = state
    delta = max(0.0, now - last_ts)
    tokens = min(capacity, tokens + delta * refill_per_sec)
    if tokens >= 1.0:
        return True, (tokens - 1.0, now), 0
    # 不足 1 个令牌：返回需等待秒数（向上取整）
    retry_after = max(1, int(-(-(1.0 - tokens) // refill_per_sec))) if refill_per_sec > 0 else 1
    return False, (tokens, now), retry_after


# Lua 原子令牌桶（与纯逻辑版语义一致；双实例并发不会多放行——面试可讲原子性）
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local ts_key = KEYS[2]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tokens = tonumber(redis.call('GET', key) or capacity)
local last = tonumber(redis.call('GET', ts_key) or now)
local delta = now - last
if delta < 0 then delta = 0 end
tokens = math.min(capacity, tokens + delta * refill)
if tokens >= 1 then
    redis.call('SET', key, tokens - 1)
    redis.call('SET', ts_key, now)
    return {1, 0}
else
    redis.call('SET', key, tokens)
    redis.call('SET', ts_key, now)
    local retry_after = math.ceil((1 - tokens) / refill)
    if retry_after < 1 then retry_after = 1 end
    return {0, retry_after}
end
"""


def check_token_bucket(
    key_hash: str,
    redis_client: RedisClient | None = None,
    capacity: int = TOKEN_BUCKET_CAPACITY,
    refill_per_min: int = TOKEN_BUCKET_REFILL_PER_MIN,
) -> tuple[bool, int]:
    """令牌桶检查（每请求一次，原子）。返回 (allowed, retry_after_seconds)。

    fail-open：Redis 不可用 → (True, 0) 放行（配额是软约束，服务不因 Redis 抖动
    拒绝合法集成方——手册 fail-open 原则；429 保护在 Redis 恢复后自动生效）。
    """
    rc = redis_client or get_redis_client()
    if not rc.available:
        return True, 0
    result = rc.eval(
        _TOKEN_BUCKET_LUA,
        2,
        _BUCKET_KEY.format(key_hash=key_hash),
        _BUCKET_TS_KEY.format(key_hash=key_hash),
        str(capacity),
        str(refill_per_min / 60.0),
        str(time.time()),
    )
    # eval 返回 None（Redis 异常）→ fail-open 放行
    if result is None:
        return True, 0
    allowed = int(result[0]) if isinstance(result, (list, tuple)) else 1
    retry_after = int(result[1]) if isinstance(result, (list, tuple)) else 0
    return bool(allowed), max(1, retry_after)


# ==================== 认证依赖：API Key 与 JWT 双轨 ====================


async def api_key_or_jwt(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """双轨认证依赖：Bearer `sk-` → API Key（动态权限 + 令牌桶限速），否则 JWT。

    这是端点级统一入口（`rbac.require_permission` 内部依赖）——集成方带 API Key
    调任何受保护端点都经过这里；限速恰好每请求一次（全局门禁只认证不限速）。
    ★ W27-D6 (B11)：认证分支（sk- 检测 + 无效 Key 401）已收敛到
    `authenticate_credentials` 单处实现，本依赖只追加限速判定。
    """
    user = await authenticate_credentials(session, credentials)
    # 限速（每请求一次，仅 API Key）：超额 429 + Retry-After（Err body 由全局处理器归一）。
    # ★ 这里的 sk- 前缀判定是"是否限速"的业务条件，非认证逻辑——认证只发生在
    #   authenticate_credentials 一处（B11 去重的对象就是认证分支）。
    if credentials is not None and credentials.credentials.startswith(API_KEY_PREFIX):
        allowed, retry_after = check_token_bucket(hash_api_key(credentials.credentials))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="api key rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
    return user
