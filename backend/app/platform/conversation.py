"""会话历史存储（W23 Day5）——`conversations` 表写入路径接通。

定位（对应《02》伴随表"conversations 多轮会话历史"）：
- 每个对话 session（thread_id）在 MySQL 落一条会话记录，归属用户与租户
- 无状态化核销：会话历史从 SQLite/进程内 → MySQL（双实例可见，W24 多轮追问数据源）
- 写入路径：kb/ops 域 chat 端点调用 `touch_conversation()`（幂等 upsert）

用法（FastAPI 请求内）：
    from app.platform.conversation import touch_conversation
    await touch_conversation(request.app.state.session_factory, thread_id=session_id,
                             user_id=current.id, tenant_id=current.tenant_id)
"""
from sqlalchemy import text

_CREATE_SQL = text(
    "INSERT INTO conversations (thread_id, user_id, tenant_id, title, metadata_json, "
    "created_at, updated_at) VALUES (:thread_id, :user_id, :tenant_id, :title, :metadata_json, "
    "CURRENT_TIMESTAMP(3), CURRENT_TIMESTAMP(3)) "
    "ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3)"
)


async def touch_conversation(session_factory, thread_id: str, user_id: int | None = None,
                             tenant_id: str | None = None, title: str | None = None,
                             metadata_json: dict | None = None) -> None:
    """会话 touch（幂等 upsert）：不存在则建会话，存在则刷新 updated_at。

    供 kb/ops 域 chat 端点调用；失败静默（会话记录尽力而为，不阻塞问答主链路）。
    """
    if not thread_id:
        return
    async with session_factory() as session:
        await session.execute(
            _CREATE_SQL,
            {
                "thread_id": thread_id[:64],
                "user_id": user_id,
                "tenant_id": tenant_id,
                "title": (title or "")[:128] or None,
                "metadata_json": metadata_json,
            },
        )
        await session.commit()


async def list_conversations(session_factory, user_id: int | None = None,
                             limit: int = 50) -> list[dict]:
    """会话列表（按最近活跃排序；用户级过滤）。返回 dict 列表（避免 ORM 重建映射）。"""
    async with session_factory() as session:
        stmt = "SELECT id, thread_id, user_id, tenant_id, title, updated_at FROM conversations"
        params: dict = {}
        if user_id is not None:
            stmt += " WHERE user_id = :user_id"
            params["user_id"] = user_id
        stmt += " ORDER BY updated_at DESC LIMIT :limit"
        params["limit"] = limit
        rows = (await session.execute(text(stmt), params)).all()
    return [dict(row._mapping) for row in rows]
