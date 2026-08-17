"""业务操作域（ops）配置——继承共享配置 + ops 特有项。

设计（对应手册 Day4"配置统一"）：
- 公共配置由 `app.shared.config` 提供，本模块 re-export（域内代码统一
  `from app.domains.ops import config` 访问）。
- ops 特有项：业务地址（mock_biz_server）、审批/幂等 DB 路径、任务队列、服务标识。
- 平台化后 JWT 相关（JWT_SECRET 等）由 `app.platform.settings` 接管，本域不再需要。
- 遗留：approvals.db / idempotency.db / audit.log 仍为文件级（Day5 数据迁移迁 MySQL）；
  默认指向 scm-copilot/data，可用环境变量指向旧 stage3-b/data 以读历史数据。
"""

import os

from app.shared.config import *  # noqa: F401,F403  # re-export 共享配置
from app.shared.config import DATA_DIR, REPORTS_DIR  # noqa: F401

# ---- 服务 ----
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8793"))
BIZ_BASE_URL = os.getenv("BIZ_BASE_URL", "http://127.0.0.1:8794")   # mock 业务系统
SERVICE_NAME = os.getenv("SERVICE_NAME", "biz-agent-b")
INSTANCE_ID = os.getenv("INSTANCE_ID", "")

# ---- 审批 / 幂等 / 审计（Day5 迁 MySQL，本周文件级）----
APPROVER = os.getenv("APPROVER", "admin")
APPROVAL_DB = os.getenv("APPROVAL_DB", str(DATA_DIR / "approvals.db"))
IDEMPOTENCY_DB = os.getenv("IDEMPOTENCY_DB", str(DATA_DIR / "idempotency.db"))
AUDIT_LOG = os.getenv("AUDIT_LOG", str(DATA_DIR / "audit.log"))

# ---- 任务队列（报表生成异步化，RQ + Redis broker；队列挂 → 同步降级）----
TASK_QUEUE_ENABLED = os.getenv("TASK_QUEUE_ENABLED", "1") == "1"
TASK_QUEUE_NAME = os.getenv("TASK_QUEUE_NAME", "report")
TASK_QUEUE_JOB_TIMEOUT = int(os.getenv("TASK_QUEUE_JOB_TIMEOUT", "300"))
TASK_QUEUE_RESULT_TTL = int(os.getenv("TASK_QUEUE_RESULT_TTL", "300"))

# ---- 观测（域内结构化日志）----
STRUCT_LOG = os.getenv("STRUCT_LOG", str(REPORTS_DIR / "struct.log.jsonl"))
STRUCT_LOG_ENABLED = os.getenv("STRUCT_LOG_ENABLED", "1") == "1"
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1") == "1"
