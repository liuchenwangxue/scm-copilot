"""模型池：DEFAULT_MODEL_POOL + 全局切换状态 + 活跃模型持久化（★ W27 Day4 拆出）。

职责边界：
- 模型清单（代码默认池 / 环境变量 LLM_MODEL_POOL 覆盖）
- 全局切换状态 `_model_pool_state`（多 provider 实例共享——并发评测统一切换）
- 最近使用模型持久化 `reports/llm_model_state.json`（避免每进程先探已耗尽模型）

依赖方向：model_pool → errors（错误分类纯函数）；被 provider 引用。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from app.shared import config

# ★ 模型池：单模型额度耗尽时自动切换（按顺序循环使用）
# W24 Day5 更新（2026-08-18）：根据 DashScope 控制台截图全量配置——已剔除 glm-5.2
# （免费额度早已耗尽，每次先探它会白白浪费一次 HTTP 调用）；
# 当前活跃模型 kimi-k2.7-code 置首位 + qwen3.7-max-2026-06-08（剩余 99%）次之；
# 剩余按字母/版本近顺序：plus-2026-05-26 / max-2026-05-20 / max-2026-05-17 /
# max-preview / plus / max / qwen3.8-2.4t-a95b / deepseek-v4-pro-0813。
# ★ 持久化最近使用的模型到 reports/llm_model_state.json：下次进程启动直接接续，
# 跳过已耗尽模型，避免每个新进程都要从 glm-5.2 探一遍再切。
DEFAULT_MODEL_POOL = [
    "kimi-k2.7-code",                  # 当前活跃（实测可用）
    "qwen3.7-max-2026-06-08",          # 剩余 99.4%，最新快照版本
    "qwen3.7-plus-2026-05-26",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-05-17",
    "qwen3.7-max-preview",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.8-2.4t-a95b",
    "deepseek-v4-pro-0813",
]

# 全局模型切换状态（多 provider 实例共享——并发评测时统一切换）
_model_pool_state: dict[str, Any] = {"idx": 0, "models": list(DEFAULT_MODEL_POOL)}


def _state_file() -> Path:
    """当前活跃模型持久化文件（★ Day5：避免每进程先探 glm-5.2）。

    与 cost_usage.jsonl 同目录，便于管理；本文件不在 .gitignore 中——但只存模型名，
    不含 Key/敏感信息（Key 仅在 .env 中，.gitignore 已保护）。
    """
    return config.REPORTS_DIR / "llm_model_state.json"


def _load_active_model() -> str | None:
    """读取上次成功调用的模型名（None = 首次启动，按池顺序）。"""
    p = _state_file()
    if not p.exists():
        return None
    try:
        import json as _json

        name = str(_json.loads(p.read_text(encoding="utf-8")).get("model") or "").strip()
        return name or None
    except Exception:  # noqa: BLE001  # 文件脏不影响业务
        return None


def _save_active_model(model: str) -> None:
    """记录当前成功调用的模型名（写盘最佳努力，失败不影响业务）。"""
    try:
        import json as _json

        p = _state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            _json.dumps(
                {"model": model, "updated_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _pool_models() -> list[str]:
    """当前模型池（可被环境变量 LLM_MODEL_POOL 覆盖，逗号分隔）。"""
    v = os.getenv("LLM_MODEL_POOL")
    if v and v.strip():
        models = [m.strip() for m in v.split(",") if m.strip()]
        if models:
            return models
    pool = _model_pool_state["models"]
    return list(pool) if isinstance(pool, list) else list(DEFAULT_MODEL_POOL)


def reorder_pool_by_active(pool: list[str]) -> tuple[list[str], int]:
    """把"上次成功调用的模型"挪到池首位（★ Day5：避免每进程先探已耗尽模型）。

    返回 (新池顺序, 该模型在新池中的下标)；持久化缺失/不匹配则原样返回。
    """
    active = _load_active_model()
    if not active or active not in pool:
        return list(pool), 0
    idx = pool.index(active)
    return pool[idx:] + pool[:idx], idx
