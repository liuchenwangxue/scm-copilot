# ★ W23 Day6：SCM Copilot 平台后端镜像
# 用法：docker compose build backend ｜ 或 cd scm-copilot && docker build -f deploy/backend/Dockerfile .
#
# 设计（对齐手册 Day6 坑 + ★ W28-D1 容器口径统一 C1）：
# - python:3.12-slim（与本地开发版本一致，体积小）
# - 通过 pyproject.toml 安装（含 fastapi/sqlalchemy/langgraph/asyncmy 等全部依赖）
# - ★ W28-D1：模型推理依赖装进镜像（torch CPU + sentence-transformers），
#   模型文件走 model-cache 卷挂载（/root/.cache，镜像不膨胀）——容器内跑真
#   embedding/reranker，评测/压测/演示三口径合一。
# - 降级哲学贯穿：模型加载失败（卷未挂/下载不全）→ 自动回退 mock embedder /
#   RuleReranker（Embedder.__init__ 内置预检），/health 可见 embedder=degraded。
# - uvicorn --workers 1：扩容靠容器数（least_conn 调度的是容器，不是进程）
# - /data /reports 为运行期数据目录（审计日志/幂等 sqlite 兜底/结构化日志），
#   无状态权威数据在 MySQL/Redis（compose 注入 DSN）
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # ★ W28-D1 坑：sentence-transformers 首次 import 会联网查 HF 版本——容器内
    #   模型已随卷挂载，禁网让"加载失败"快速反馈（降级 mock）而非长时间卡住
    SENTENCE_TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

# ★ W28-D1：模型推理依赖（放 COPY 之前——层缓存：业务代码改动不触发模型包重装）。
#   torch 用 CPU index（体积最小，无 CUDA 库）；sentence-transformers 带入
#   transformers/tokenizers。bge-reranker 交叉编码直接走 transformers 前向（reranker.py）。
RUN pip install --no-cache-dir \
        torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir sentence-transformers

COPY backend ./backend
COPY pyproject.toml ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# 运行期可写目录（audit.log / idempotency.db / struct.log 等）
RUN mkdir -p /data /reports

EXPOSE 8795

# workers=1（手册坑：双实例扩容靠容器数量，非进程数）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8795", "--workers", "1"]
