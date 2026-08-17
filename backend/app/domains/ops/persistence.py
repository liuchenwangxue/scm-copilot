"""★ A8 生产级持久化（W19 Day4 欠账落地）：LangGraph SqliteSaver + 共享 sqlite

W2 只用过 InMemorySaver（进程内存，重启即丢）；Day4 升级 SqliteSaver：
- checkpointer：多轮会话 / 审批中断状态落 sqlite（进程重启不丢）
- 审批单表 + 幂等表也落在同一个 db（biz_agent.db），事务一致性统一
- Day5 升级 AsyncSqliteSaver：图含 async 节点（intent/respond 调 LLM），
  sync SqliteSaver 不支持 async 方法（aget_tuple 等），必须用 async 版（aiosqlite）

用法（Day5 组图时）：
    from persistence import get_async_checkpointer
    checkpointer = get_async_checkpointer()   # AsyncSqliteSaver
    graph = graph.compile(checkpointer=checkpointer)
    result = await graph.ainvoke(input, config={"configurable": {"thread_id": session_id}})
"""
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# 共享数据库路径（审批单 / 幂等 / checkpointer 状态共用）
# ★ W23 Day4：指向 scm-copilot/data（与原 stage3-b/data 分离；Day5 数据迁移切 MySQL 后废弃）
DATA_DIR = Path(__file__).resolve().parents[4] / "data"
DB_PATH = DATA_DIR / "biz_agent.db"

# checkpointer 单例缓存（进程内复用连接，避免多开连接写冲突）
_checkpointer = None
_ctx = None       # 上下文管理器必须持有引用，否则被 GC 会关闭连接
_async_checkpointer = None
_async_ctx = None


def get_checkpointer() -> SqliteSaver:
    """同步版 SqliteSaver（Day4，纯 sync 图用）。"""
    global _checkpointer, _ctx
    if _checkpointer is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _ctx = SqliteSaver.from_conn_string(str(DB_PATH))
        _checkpointer = _ctx.__enter__()
    return _checkpointer


async def get_async_checkpointer() -> AsyncSqliteSaver:
    """异步版 AsyncSqliteSaver（Day5，async 图用；进程内单例）。"""
    global _async_checkpointer, _async_ctx
    if _async_checkpointer is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _async_ctx = AsyncSqliteSaver.from_conn_string(str(DB_PATH))
        _async_checkpointer = await _async_ctx.__aenter__()
    return _async_checkpointer


def reset_checkpointer():
    """测试用：清空单例缓存（让下个 get_* 重建）。"""
    global _checkpointer, _ctx, _async_checkpointer, _async_ctx
    if _checkpointer is not None:
        from contextlib import suppress
        with suppress(Exception):
            _ctx.__exit__(None, None, None)
    if _async_checkpointer is not None:
        from contextlib import suppress
        with suppress(Exception):
            _async_ctx.__aexit__(None, None, None)
    _checkpointer = None
    _ctx = None
    _async_checkpointer = None
    _async_ctx = None
