# -*- coding: utf-8 -*-
"""Docker 启动包装器（W20 Day1，W23 Day6 随 SCM Copilot 部署复制）：
在 uvicorn 同进程内先灌种子数据。

背景：mock 业务系统数据是进程内内存态（db.orders 等模块级 dict）。
W19 用 `python main.py` 启动（main() 里调 db.init_data()）；
Docker 用 uvicorn 直接启动 main:app 会跳过 init_data → 空库。

方案：用 uvicorn 加载本模块（run:app），导入时先 init_data() 再暴露 app——
与 uvicorn 同进程，数据对请求可见。

用法：uvicorn run:app --host 0.0.0.0 --port 8794
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

db.init_data()

from main import app  # noqa: E402
