"""平台全局配置（pydantic-settings，env 驱动）。

- 环境变量前缀 SCM_，如 SCM_PLATFORM_DSN 覆盖 PLATFORM_DSN
- 双库 DSN：scm_platform（平台库）+ scm_biz（业务库，W24 用）
- 默认指向本地 MySQL（deploy/docker-compose.yml，宿主端口 13306）
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SCM Copilot 平台配置。"""

    # ---- 应用 ----
    app_name: str = "SCM Copilot"
    debug: bool = False

    # ---- 双库 DSN ----
    # 平台库：身份 / 审批 / 审计 / 调度（W23 主战场）
    platform_dsn: str = (
        "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_platform?charset=utf8mb4"
    )
    # 业务库：NL2SQL 数据域（W24 使用，本周仅预置）
    biz_dsn: str = (
        "mysql+asyncmy://root:root123@127.0.0.1:13306/scm_biz?charset=utf8mb4"
    )

    # ---- JWT（Day3 使用）----
    jwt_secret: str = "dev-secret-change-me"
    jwt_access_minutes: int = 15
    jwt_refresh_minutes: int = 60 * 24

    # ---- Redis（W24 前迁完缓存 keys，Day5 起强依赖）----
    redis_url: str = "redis://127.0.0.1:16380/0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SCM_",
        extra="ignore",
    )


settings = Settings()
