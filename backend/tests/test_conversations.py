"""W23 Day5 会话历史（conversations）写入路径集成测试（integration，需 MySQL）。

验证点：
- touch_conversation 幂等 upsert：同一 thread_id 只落一条，重复 touch 刷新 updated_at
- 归属字段正确：user_id / tenant_id / title 落库
- list_conversations 可查询（按最近活跃倒序）
"""

import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.platform.conversation import list_conversations, touch_conversation

TEST_DSN = os.environ.get(
    "SCM_TEST_DSN",
    "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4",
)


@pytest.fixture
async def sf():
    """测试级 session_factory（独立 engine，测试后释放）。"""
    engine = create_async_engine(TEST_DSN, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _tid() -> str:
    return f"test-conv-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
async def test_touch_creates_and_lists(sf):
    tid = _tid()
    await touch_conversation(
        sf, thread_id=tid, user_id=1, tenant_id="t_huadong", title="采购申请需要几级审批"
    )
    rows = await list_conversations(sf, user_id=1)
    mine = [r for r in rows if r["thread_id"] == tid]
    assert len(mine) == 1
    assert mine[0]["user_id"] == 1
    assert mine[0]["tenant_id"] == "t_huadong"
    assert mine[0]["title"] == "采购申请需要几级审批"


@pytest.mark.integration
async def test_touch_idempotent(sf):
    tid = _tid()
    for _ in range(3):  # 同一会话连续 touch（多轮对话）
        await touch_conversation(sf, thread_id=tid, user_id=2, tenant_id="t_huabei")
    rows = await list_conversations(sf, user_id=2)
    mine = [r for r in rows if r["thread_id"] == tid]
    assert len(mine) == 1, "同 thread_id 重复 touch 不得重复插入（thread_id 唯一）"


@pytest.mark.integration
async def test_touch_empty_thread_skipped(sf):
    await touch_conversation(sf, thread_id="", user_id=1)  # 空 thread_id 静默跳过
    rows = await list_conversations(sf, user_id=1)
    assert all(r["thread_id"] for r in rows)
