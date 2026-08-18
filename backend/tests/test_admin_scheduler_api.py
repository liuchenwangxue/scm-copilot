"""W25 Day2 调度面板 API 测试：权限闸 / 未启用降级 / 真实调度器（integration）。

覆盖手册 Day2 下午：
- `GET /api/admin/scheduler/jobs`：任务面板（六任务 + 上次运行 + next_run）
- `POST /api/admin/scheduler/jobs/{name}/trigger`：手动触发 + 写审计
- 权限：仅 `admin:scheduler:manage`（operator 403）；scheduler 未启用 → 503
"""

import pytest

pytestmark = pytest.mark.integration

# 与 scripts/seed_platform.py 一致（固定 seed 凭证；测试库已 seed）
PLAIN_PASSWORD = "Passw0rd!"


def tenant_user(role: str, tenant: str = "t_huadong") -> str:
    return f"{role}_{tenant}"


def _login(client, username: str) -> dict:
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": PLAIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_scheduler_jobs_forbidden_for_operator(client):
    """非 admin（operator 无 admin:scheduler:manage）→ 403。"""
    headers = _login(client, tenant_user("operator"))
    assert client.get("/api/admin/scheduler/jobs", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/admin/scheduler/jobs/kb_increment_sync/trigger",
            headers=headers,
        ).status_code
        == 403
    )


def test_scheduler_jobs_503_when_scheduler_disabled(client):
    """调度器未启用（CI/单测环境）→ 503 而非 500（面板降级可预期）。"""
    headers = _login(client, tenant_user("admin"))
    assert client.get("/api/admin/scheduler/jobs", headers=headers).status_code == 503
    assert (
        client.post(
            "/api/admin/scheduler/jobs/kb_increment_sync/trigger", headers=headers
        ).status_code
        == 503
    )


def test_scheduler_panel_with_real_scheduler(client):
    """真实调度器：GET 面板六任务 + POST 手动触发 + 审计落库。"""
    from app.platform.scheduler import PlatformScheduler
    from app.platform.settings import settings

    svc = PlatformScheduler(
        jobstore_dsn=settings.jobstore_dsn,
        session_factory=client.app.state.session_factory,
        instance_id="panel-test",
    )
    # AsyncIOScheduler.start() 需要 running loop → 经 portal 在 TestClient 的 loop 内执行
    client.portal.call(svc.start)
    client.app.state.scheduler = svc
    try:
        headers = _login(client, tenant_user("admin"))

        # ---- GET 面板 ----
        resp = client.get("/api/admin/scheduler/jobs", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["scheduler"]["running"] is True
        names = [j["name"] for j in body["jobs"]]
        assert len(body["jobs"]) == 6
        assert "kb_increment_sync" in names
        job = next(j for j in body["jobs"] if j["name"] == "kb_increment_sync")
        assert job["cron"] == "*/5 * * * *"
        assert job["enabled"] is True
        assert job["next_run_time"] is not None
        assert job["last_run"] is None or job["last_run"]["status"] in {
            "success",
            "failed",
            "skipped",
            "running",
        }

        # ---- POST 手动触发（审计留痕） ----
        resp = client.post("/api/admin/scheduler/jobs/kb_increment_sync/trigger", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        assert resp.json()["audited"] is True

        # 审计落库：admin.scheduler.trigger + actor = admin 用户
        # TestClient 是同步接口，异步 DB 查询经 portal 在 lifespan 同 loop 执行
        async def _query_audit():
            from sqlalchemy import text

            async with client.app.state.session_factory() as s:
                return list(
                    (
                        await s.execute(
                            text(
                                "SELECT event, actor, detail FROM audit_logs "
                                "WHERE event = 'admin.scheduler.trigger' "
                                "ORDER BY id DESC LIMIT 1"
                            )
                        )
                    ).all()
                )

        rows = client.portal.call(_query_audit)
        assert rows, "trigger 应写审计"
        event, actor, detail = rows[0]
        assert event == "admin.scheduler.trigger"
        assert actor == tenant_user("admin")
        assert '"kb_increment_sync"' in (detail or {})
    finally:
        client.app.state.scheduler = None
        client.portal.call(lambda: svc.shutdown(wait=False))
