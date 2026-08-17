"""共享层配置（W23 Day4 双域并入）——app.shared 各模块的统一配置入口。

设计背景（对应手册 Day4"配置统一"）：
- stage3-a/stage3-b 各有一份 config.py，公共项高度重叠（LLM / Redis / 观测 / OTEL）。
- 迁移后 shared 层（llm / rag / reliability / obs）只依赖本模块，不跨域 import。
- 域特有配置（kb 的 Qdrant/语义路由、ops 的业务地址/审批/队列）保留在
  `app.domains.kb.config` / `app.domains.ops.config`，本模块提供基础项 + 数据路径。

环境变量前缀与原项目一致（LLM_ / REDIS_ / QDRANT_ / OTEL_ 等），
也可用 SCM_ 前缀（由 platform/settings.py 统一管理）——本模块读取优先级：
显式 os.getenv 值 > 默认值；生产通过 compose/env 注入。
"""

import os
from pathlib import Path

# ---- 极简 .env 加载（无三方依赖）：环境变量优先级更高 ----
_env_file = Path(__file__).resolve().parents[3] / ".env"  # scm-copilot/.env
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---- 路径（指向 scm-copilot 根目录；数据目录可被 KB_DATA_DIR 覆盖指向旧库）----
ROOT = Path(__file__).resolve().parents[3]               # scm-copilot/
DATA_DIR = Path(os.getenv("KB_DATA_DIR", str(ROOT / "data")))
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", str(ROOT / "reports")))
DOCS_DIR = DATA_DIR / "docs"

# ---- LLM（mock | real，与 stage3 同构）----
# ★ mock-first：默认 mock（无 Key 环境服务不崩）；配好 Key 后设 LLM_PROVIDER=real
#   （stage3 默认 real 是因为其 .env 已配 Key；平台化后无 Key 自动降级为开发安全值）
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")   # mock | real
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_TIMEOUT = os.getenv("LLM_TIMEOUT", "30")
LLM_DEGRADE_TO_MOCK = os.getenv("LLM_DEGRADE_TO_MOCK", "1")  # real 失败降级 mock，可关
# 模型池（额度耗尽自动切换，逗号分隔；不配则用代码默认池）
LLM_MODEL_POOL = os.getenv("LLM_MODEL_POOL", "")

# ---- 观测（LangFuse 默认关，配好自托管平台再开）----
LLM_ENABLE_LANGFUSE = os.getenv("LLM_ENABLE_LANGFUSE", "0") == "1"
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3300")

# ---- 成本预算（A3 超预算降级）----
SESSION_BUDGET_YUAN = float(os.getenv("SESSION_BUDGET_YUAN", "0.5"))
COST_PRICE_INPUT = float(os.getenv("COST_PRICE_INPUT", "2"))      # ¥/百万 token 输入
COST_PRICE_OUTPUT = float(os.getenv("COST_PRICE_OUTPUT", "8"))    # ¥/百万 token 输出

# ---- Redis（幂等 / 缓存 / 分布式锁；fail-open）----
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:16380/0")
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "1") == "1"
REDIS_SOCKET_TIMEOUT = float(os.getenv("REDIS_SOCKET_TIMEOUT", "1.0"))
REDIS_IDEM_TTL = int(os.getenv("REDIS_IDEM_TTL", "300"))
REDIS_CACHE_TTL = int(os.getenv("REDIS_CACHE_TTL", "60"))
REDIS_LOCK_TTL = int(os.getenv("REDIS_LOCK_TTL", "30"))

# ---- Qdrant（复用 stage3 W5 容器，端口 6333）----
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_TIMEOUT = int(os.getenv("QDRANT_TIMEOUT", "30"))
SCM_COLLECTION = os.getenv("SCM_COLLECTION", "scm_kb_v1")

# ---- Embedding（复用 W3/W5 的 bge-small-zh-v1.5 缓存）----
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "512"))
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# ---- 检索数据文件（kb 域使用；默认指向 scm-copilot/data，旧数据在 stage3-a/data）----
CHUNKS_FILE = os.getenv("CHUNKS_FILE", str(DATA_DIR / "chunks_title.json"))
BM25_CACHE_FILE = os.getenv("BM25_CACHE_FILE", str(DATA_DIR / "bm25_index_cache.json"))
QA_EVAL_FILE = os.getenv("QA_EVAL_FILE", str(DATA_DIR / "qa_eval_set.json"))
SEMANTIC_ROUTER_SAMPLES_FILE = os.getenv(
    "SEMANTIC_ROUTER_SAMPLES_FILE", str(DATA_DIR / "semantic_router_samples.json"))
SEMANTIC_ROUTER_VECTORS_FILE = os.getenv(
    "SEMANTIC_ROUTER_VECTORS_FILE", str(DATA_DIR / "semantic_router_vectors.json"))

# ---- 语义路由（kb 域使用；阈值分开调：rag 宽容、tool/chat 从严）----
SEMANTIC_ROUTER_THRESHOLDS = {"rag": 0.60, "tool": 0.80, "chat": 0.85}
SEMANTIC_ROUTER_FALLBACK = "rag"
SEMANTIC_ROUTER_PROTOTYPES = {
    "rag": [
        "采购申请需要经过哪几级审批",
        "采购金额超过多少必须招标采购",
        "供应商准入需要提交哪些资质材料",
        "供应商年度评估从哪几个维度打分",
        "供应商发生重大变更后多久内必须报备",
        "库存盘点多久进行一次",
        "库存差异超过多少需要上报",
        "货款结算的账期和付款条件有哪些要求",
        "合同变更需要走什么审批流程",
        "物流运输延迟如何赔付",
        "质量检验不合格如何退回",
        "组织部门职能分工是怎么规定的",
    ],
    "tool": [
        "帮我查一下订单 PO-0001 的状态",
        "查询订单 PO-0002 现在到哪了",
        "把订单 PO-0002 的交期改到月底",
        "把订单 PO-0003 的金额改成 9500",
        "取消这笔采购订单 PO-0004",
        "作废订单 PO-0005",
        "生成上个月的库存对账报表",
        "汇总一下这个月的订单报表",
    ],
    "chat": [
        "你好，很高兴认识你",
        "你是谁，你能做什么",
        "谢谢你的回答",
        "再见，下次聊",
        "今天天气怎么样",
        "能陪我聊聊天吗",
        "你真厉害",
        "周末愉快",
        "嗯，好的，知道了",
        "早上好",
    ],
}

# ---- 语义缓存（宁不命中不漏命中）----
SEMANTIC_CACHE_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))
SEMANTIC_CACHE_MAX_SIZE = int(os.getenv("SEMANTIC_CACHE_MAX_SIZE", "2000"))
SEMANTIC_CACHE_VERSION = os.getenv("SEMANTIC_CACHE_VERSION", "kb-v1")

# ---- 可观测性（结构化日志 + Prometheus 指标 + OTEL）----
SERVICE_NAME = os.getenv("SERVICE_NAME", "scm-copilot")
INSTANCE_ID = os.getenv("INSTANCE_ID", "")
STRUCT_LOG_ENABLED = os.getenv("STRUCT_LOG_ENABLED", "1") == "1"
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1") == "1"
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "1") == "1"
OTEL_EXPORTER = os.getenv("OTEL_EXPORTER", "console")
OTEL_OTLP_ENDPOINT = os.getenv("OTEL_OTLP_ENDPOINT", "")

# ---- API 限流（自研固定窗口中间件，阈值=压测 P95 依据）----
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1") == "1"
RATE_LIMIT_GLOBAL = os.getenv("RATE_LIMIT_GLOBAL", "300")
RATE_LIMIT_PER_KEY = os.getenv("RATE_LIMIT_PER_KEY", "20")
RATE_LIMIT_WINDOW = os.getenv("RATE_LIMIT_WINDOW", "60")

# ---- 目录初始化 ----
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
