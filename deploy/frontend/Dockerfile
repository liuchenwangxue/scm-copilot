# ★ W28 Day2：SCM Copilot 前端镜像（Gradio 三页）
# 用法：docker compose build gradio ｜ docker build -f deploy/frontend/Dockerfile .
#
# 设计（对齐 W28 手册 Day2）：
# - python:3.12-slim（与 backend 同基镜像，体积可控）
# - 前端直接走 SCM SDK 0.2.0（dogfooding：SDK 即前端的网络层，429 自动退避内置）
# - SDK 从本地 sdk/ 目录安装（与 TestPyPI 0.2.0 同源；也可改 pip install scm-copilot-client）
# - gradio 单进程（Gradio 自带 queue；演示无需多 worker）
# - 环境变量：SCM_BASE_URL（后端 nginx https 入口）、SCM_FRONTEND_PORT、SCM_ROOT_PATH
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 前端无模型推理，无网络需求（HF 禁网避免 import 卡顿）
    SENTENCE_TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

# 前端依赖（放 COPY 之前——层缓存：业务代码改动不触发依赖重装）
COPY sdk ./sdk
COPY frontend/requirements.txt ./frontend/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ./sdk \
    && pip install --no-cache-dir -r frontend/requirements.txt

COPY frontend ./frontend

WORKDIR /app/frontend
EXPOSE 7860

CMD ["python", "app.py"]
