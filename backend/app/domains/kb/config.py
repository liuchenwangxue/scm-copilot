"""知识问答域（kb）配置——继承共享配置 + kb 特有项。

设计（对应手册 Day4"配置统一"）：
- 公共配置（LLM / Redis / Qdrant / 观测 / 语义路由阈值等）由 `app.shared.config` 提供，
  本模块 re-export（域内代码统一 `from app.domains.kb import config` 访问）。
- kb 特有项：语义路由/缓存开关、CRAG、日志路径、服务标识（env 可覆盖）。
- 遗留说明：stage3-a 的 `config.py` 中 JWT 相关项（JWT_SECRET 等）在平台化后由
  `app.platform.settings` 接管（Day3 认证已统一），本域不再需要。
"""

import os

from app.shared.config import *  # noqa: F401,F403  # re-export 共享配置
from app.shared.config import REPORTS_DIR  # noqa: F401  (显式列出常用项便于 IDE)

# ---- kb 特有开关（默认与原 stage3-a 一致）----
SEMANTIC_ROUTER_ENABLED = os.getenv("SEMANTIC_ROUTER_ENABLED", "1") == "1"
SEMANTIC_CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "1") == "1"
CRAG_ENABLED = os.getenv("CRAG_ENABLED", "1") == "1"

# ---- 服务标识 / 观测（域内结构化日志）----
SERVICE_NAME = os.getenv("SERVICE_NAME", "kb-agent-a")
INSTANCE_ID = os.getenv("INSTANCE_ID", "")
STRUCT_LOG = os.getenv("STRUCT_LOG", str(REPORTS_DIR / "struct.log.jsonl"))
STRUCT_LOG_ENABLED = os.getenv("STRUCT_LOG_ENABLED", "1") == "1"
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1") == "1"
