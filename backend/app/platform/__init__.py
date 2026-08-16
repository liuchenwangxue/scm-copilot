"""平台基座：auth / rbac / audit / scheduler / quota（W23 起逐日建设）。

models 在此 re-export，便于 `from app.platform.models import Base` 且 alembic
autogenerate 能感知所有表。
"""
