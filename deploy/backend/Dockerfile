# ★ W23 Day6：SCM Copilot 平台后端镜像
# 用法：docker compose build backend ｜ 或 cd scm-copilot && docker build -f deploy/backend/Dockerfile .
#
# 设计（对齐手册 Day6 坑）：
# - python:3.12-slim（与本地开发版本一致，体积小）
# - 通过 pyproject.toml 安装（含 fastapi/sqlalchemy/langgraph/asyncmy 等全部依赖）
# - 不装 sentence-transformers/torch/rank_bm25（模型推理不在容器内做；
#   kb 压测走语义路由规则层，mock LLM 兜底——LLM_PROVIDER=mock 由 compose 注入）
# - uvicorn --workers 1：扩容靠容器数（least_conn 调度的是容器，不是进程）
# - /data /reports 为运行期数据目录（审计日志/幂等 sqlite 兜底/结构化日志），
#   无状态权威数据在 MySQL/Redis（compose 注入 DSN）
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY backend ./backend
COPY pyproject.toml ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# 运行期可写目录（audit.log / idempotency.db / struct.log 等）
RUN mkdir -p /data /reports

EXPOSE 8795

# workers=1（手册坑：双实例扩容靠容器数量，非进程数）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8795", "--workers", "1"]
