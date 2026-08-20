"""W23 Day1 最小骨架测试：/health 探活 + MySQL 连通性（CI 可跑）。

- test_health_endpoint：用 TestClient 打 /health（不依赖 MySQL 时返回 degraded）
- test_mysql_connectivity：标记 integration——DSN 由 SCM_TEST_DSN 覆盖：
  CI 用 service container 映射到 job 本机的 127.0.0.1:3306（实测 VM runner
  解析不了 service 名 `mysql`），本地默认 docker compose 的 13306
"""

import os

import pytest

# 连通性测试的 DSN 可被环境变量覆盖（CI 用 mysql:3306，本地默认 13306）
TEST_DSN = os.environ.get(
    "SCM_TEST_DSN",
    "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4",
)


def test_health_endpoint():
    """/health 始终 200，结构正确；DB 状态随环境（无 DB 时应为 down/degraded）。

    ★ W25 Day1：新增 scheduler 字段（running/off）——调度器随进程启动，
    MySQL 不可用时 fail-open 降级为 off，health 仍 200 不阻塞主服务。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {
            "status",
            "db",
            "scheduler",
            "embedder",
            "reranker",
            "semantic_cache",
        }
        assert body["status"] in ("ok", "degraded")
        assert body["db"] in ("up", "down")
        assert body["scheduler"] in ("running", "off")
        # ★ W28-D1：模型状态可见（测试环境 conftest 设 SCM_EMBEDDER=mock/SCM_RERANKER=rule）
        assert body["embedder"] in ("real", "mock", "mock_degraded", "pending")
        assert body["reranker"] in ("rule", "bge", "bge-failed→rule", "pending")
        assert body["semantic_cache"] in ("on", "off")


@pytest.mark.integration
async def test_mysql_connectivity():
    """真实 MySQL 连通：SELECT 1 + 时区验证（TZ=Asia/Shanghai → UTC+8）。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TEST_DSN)
    try:
        async with engine.connect() as conn:
            one = await conn.scalar(text("SELECT 1"))
            assert one == 1
            tz_offset = await conn.scalar(
                text("SELECT TIMESTAMPDIFF(HOUR, UTC_TIMESTAMP(), NOW())")
            )
            assert tz_offset == 8, f"TZ 错误，期望 UTC+8，实际 {tz_offset}h"
    finally:
        await engine.dispose()
