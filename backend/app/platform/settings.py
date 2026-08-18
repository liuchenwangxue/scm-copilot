"""平台全局配置（pydantic-settings，env 驱动）。

- 环境变量前缀 SCM_，如 SCM_PLATFORM_DSN 覆盖 PLATFORM_DSN
- 双库 DSN：scm_platform（平台库）+ scm_biz（业务库，W24 用）
- 默认指向本地 MySQL（deploy/docker-compose.yml，宿主端口 13306）
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SCM Copilot 平台配置。"""

    # ---- 应用 ----
    app_name: str = "SCM Copilot"
    debug: bool = False

    # ---- 双库 DSN ----
    # 平台库：身份 / 审批 / 审计 / 调度（W23 主战场）
    platform_dsn: str = "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4"
    # 业务库：NL2SQL 数据域（W24 使用，本周仅预置）
    biz_dsn: str = "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_biz?charset=utf8mb4"
    # 业务库只读账号（★ W24 Day2：NL2SQL 沙箱执行专用，与 sqlglot 闸构成纵深防御双保险）
    # 仅 GRANT SELECT，写操作被 MySQL 拒绝（ERROR 1142）；CI 用 SCM_BIZ_RO_DSN 覆盖
    biz_ro_dsn: str = (
        "mysql+asyncmy://nl2sql_ro:ro_pass_2026_dev@127.0.0.1:13306/scm_biz?charset=utf8mb4"
    )

    # ---- JWT（Day3 使用）----
    jwt_secret: str = "dev-secret-change-me"
    jwt_access_minutes: int = 15
    jwt_refresh_minutes: int = 60 * 24

    # ---- Redis（W24 前迁完缓存 keys，Day5 起强依赖；★ W25 Day5 修正端口 16381）----
    redis_url: str = "redis://127.0.0.1:16381/0"

    # ---- 调度器（W25 Day1：APScheduler + MySQL job store）----
    # job store 用【同步】SQLAlchemy engine（pymysql），与平台 asyncmy 连接池独立
    # （手册坑：MySQLJobStore 与平台库 DSN 分开；表 apscheduler_jobs 内建在 scm_platform）
    # 空=未覆盖时由 platform_dsn 前缀替换派生（asyncmy → pymysql），
    # 可用 SCM_JOBSTORE_DSN 显式覆盖（本地默认 compose 13306 即可不设）
    jobstore_dsn: str = ""
    # 调度时区：与 MySQL TZ（Asia/Shanghai）一致，cron 表达式按本地时间触发
    scheduler_timezone: str = "Asia/Shanghai"
    # 实例标识：双实例互斥观测用（compose 注入 INSTANCE_ID=backend-a1/a2；本地默认 local）
    instance_id: str = "local"
    # 调度器启停开关（CI 纯单测环境可关，避免 job store 初始化干扰）
    scheduler_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SCM_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _derive_jobstore_dsn(self) -> "Settings":
        """jobstore_dsn 未显式覆盖时，从 platform_dsn 派生（asyncmy → pymysql 同步驱动）。

        保证 CI（SCM_TEST_DSN 覆盖平台 DSN）与本地（compose 13306）job store 自动同库，
        无需重复维护两个 DSN。
        """
        if not self.jobstore_dsn:
            self.jobstore_dsn = self.platform_dsn.replace(
                "mysql+asyncmy://", "mysql+pymysql://", 1
            )
        return self


settings = Settings()
