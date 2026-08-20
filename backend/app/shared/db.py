"""★ 读写分离路由开关（W28 Day5，C7/B8 项）——ADR-010 的代码落点。

背景（对应 ADR-010）：
- 现状：平台库/业务库已分（alembic vs alembic_biz 双库）；**读写未分**。
- 演进路径：单库 → 读写分离（本开关）→ 拆库（B8 backlog，见 ADR-010 §五）。

设计（最小 20 行核心 + 可单测）：
- `DbRouter`：持有 write_dsn（主）+ read_dsn（只读副本，可空）。
- `dsn_for(operation)`：读操作 → read_dsn（**配置了才用**）；写操作 → write_dsn。
- 本机无副本（read_dsn 空或 == write_dsn）→ 恒返回 write_dsn——**零行为差异**，
  灰度开关：上线只读副本前把 `SCM_DB_RO_DSN` 设为同 DSN 即可预演路由逻辑。
- 纯逻辑无 IO：不建 engine/不连库，单测用两个 DSN 断言路由正确。

接入点（main.py lifespan）：
- write_factory → app.state.session_factory（现状不变，写路径主 DSN）
- read_factory → app.state.read_session_factory（有副本才建；NL2SQL executor 与
  报表查询走 RO——executor 已用 biz_ro_dsn 只读账号，本开关统一路由语义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

OP_READ = "read"
OP_WRITE = "write"
READ_OPS = {OP_READ, "select", "query"}


@dataclass
class DbRouter:
    """读写分离路由：读走只读副本（有才用），写恒走主。"""

    write_dsn: str
    read_dsn: str = ""

    def dsn_for(self, operation: str = OP_READ) -> str:
        """按操作类型返回应使用的 DSN。

        - 读操作且有独立只读副本（read_dsn 非空且 != 主）→ read_dsn
        - 其余（写操作 / 无副本 / 副本 == 主）→ write_dsn（零行为差异）
        """
        if operation in READ_OPS and self.read_dsn and self.read_dsn != self.write_dsn:
            return self.read_dsn
        return self.write_dsn

    @property
    def has_read_replica(self) -> bool:
        """是否存在**有效**只读副本（配置且与主不同）。"""
        return bool(self.read_dsn) and self.read_dsn != self.write_dsn


@dataclass
class DbRouters:
    """多库路由集合（平台库 + 业务库各一个 router）。"""

    platform: DbRouter = field(default_factory=lambda: DbRouter("", ""))
    biz: DbRouter = field(default_factory=lambda: DbRouter("", ""))


def make_routers(
    platform_dsn: str,
    platform_ro_dsn: str = "",
    biz_dsn: str = "",
    biz_ro_dsn: str = "",
) -> DbRouters:
    """从 settings 构造双库路由（默认 RO 空 = 无副本，零行为差异）。"""
    return DbRouters(
        platform=DbRouter(write_dsn=platform_dsn, read_dsn=platform_ro_dsn),
        biz=DbRouter(write_dsn=biz_dsn, read_dsn=biz_ro_dsn),
    )
