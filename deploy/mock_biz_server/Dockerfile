# ★ W23 Day6：mock 业务系统镜像（ops 域工具调用目标）
# 用法：docker compose build mock-biz
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY . .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn

EXPOSE 8794

# run.py：先 db.init_data() 再暴露 app（否则 Docker 下空库）
CMD ["uvicorn", "run:app", "--host", "0.0.0.0", "--port", "8794", "--log-level", "warning"]
