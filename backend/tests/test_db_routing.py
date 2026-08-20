"""★ W28 Day5（C7/B8，ADR-010）：读写分离路由开关单测。

覆盖手册验收："RO 路由单测绿（两个 DSN 断言路由正确）"。
纯逻辑无 IO（DbRouter 不建 engine 不连库）——CI 可跑。
"""
from app.shared.db import DbRouter, make_routers

WRITE_DSN = "mysql+asyncmy://root:pw@mysql:3306/scm_platform?charset=utf8mb4"
RO_DSN = "mysql+asyncmy://ro:pw@mysql-ro:3306/scm_platform?charset=utf8mb4"


def test_read_routes_to_ro_when_configured():
    """配置了只读副本：读操作走 RO，写操作恒走主。"""
    r = DbRouter(write_dsn=WRITE_DSN, read_dsn=RO_DSN)
    assert r.dsn_for("read") == RO_DSN
    assert r.dsn_for("write") == WRITE_DSN
    assert r.has_read_replica is True


def test_read_falls_back_to_write_when_no_replica():
    """无只读副本（read_dsn 空）：读也走主 DSN——零行为差异。"""
    r = DbRouter(write_dsn=WRITE_DSN)
    assert r.dsn_for("read") == WRITE_DSN
    assert r.dsn_for("write") == WRITE_DSN
    assert r.has_read_replica is False


def test_same_dsn_no_replica():
    """read_dsn == write_dsn：视为无副本（本机部署常见：DSN 相同零差异）。"""
    r = DbRouter(write_dsn=WRITE_DSN, read_dsn=WRITE_DSN)
    assert r.dsn_for("read") == WRITE_DSN
    assert r.has_read_replica is False


def test_semantic_aliases_for_read():
    """读语义别名（select/query）与 read 一致走 RO。"""
    r = DbRouter(write_dsn=WRITE_DSN, read_dsn=RO_DSN)
    assert r.dsn_for("select") == RO_DSN
    assert r.dsn_for("query") == RO_DSN


def test_make_routers_from_settings_shape():
    """make_routers 构造双库路由：platform/biz 各一，默认无副本。"""
    routers = make_routers(
        platform_dsn=WRITE_DSN,
        platform_ro_dsn="",
        biz_dsn="mysql+asyncmy://root:pw@mysql:3306/scm_biz?charset=utf8mb4",
        biz_ro_dsn="",
    )
    assert routers.platform.dsn_for("read") == WRITE_DSN
    assert routers.platform.has_read_replica is False
    assert routers.biz.has_read_replica is False


def test_make_routers_with_replica():
    """make_routers 传 RO DSN → platform 路由到 RO。"""
    routers = make_routers(platform_dsn=WRITE_DSN, platform_ro_dsn=RO_DSN)
    assert routers.platform.has_read_replica is True
    assert routers.platform.dsn_for("read") == RO_DSN
    assert routers.platform.dsn_for("write") == WRITE_DSN
