"""成本记录：token/usage 解析 + cost_usage.jsonl + Prometheus 指标（★ W27 Day4 拆出）。

职责边界：
- `_parse_usage(payload)`：解析 usage（兼容推理模型 reasoning_tokens）
- `_log_cost(usage, model, tag)`：追加 cost_usage.jsonl + Prometheus Counter
  （★ W26 Day1：scm_llm_tokens_total / scm_llm_cost_yuan_total，Grafana "成本看板" 数据源）
- `_estimate_cost_yuan(usage)`：按单价估算本轮成本（¥/百万 token，W24 成本口径）

依赖方向：cost → config + obs.metrics（懒导入）；被 provider 引用。
"""

from __future__ import annotations

import json
import time

from app.shared import config


def _parse_usage(payload: dict) -> dict:
    """解析 usage，兼容推理模型（reasoning_tokens 计入 completion）。"""
    u = payload.get("usage") or {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens", 0)),
        "completion_tokens": int(u.get("completion_tokens", 0)),
        "total_tokens": int(u.get("total_tokens", 0)),
        "reasoning_tokens": int((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)),
    }


def _estimate_cost_yuan(usage: dict) -> float:
    """按单价估算本轮成本（¥/百万 token，W24 成本口径）。"""
    try:
        prompt = float(usage.get("prompt_tokens", 0) or 0)
        completion = float(usage.get("completion_tokens", 0) or 0)
        input_price = float(config.COST_PRICE_INPUT)
        output_price = float(config.COST_PRICE_OUTPUT)
        return round((prompt * input_price + completion * output_price) / 1_000_000, 6)
    except Exception:
        return 0.0


def _inc_cost_metrics(model: str, usage: dict, cost_yuan: float) -> None:
    """把 token/成本写入 Prometheus（fail-open，观测旁路）。"""
    try:
        from app.shared.obs.metrics import inc_llm_usage
        inc_llm_usage(
            model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_yuan=cost_yuan,
        )
    except Exception:
        pass


def _log_cost(usage: dict, model: str, tag: str) -> None:
    """成本/usage 记录：追加 JSON line（Day5 成本实测的数据源）。

    ★ W26 Day1：同步记录 Prometheus Counter（scm_llm_tokens_total /
    scm_llm_cost_yuan_total，label=model）——Grafana "成本看板" 面板
    token 用量按模型 / 单轮成本 / 日预算水位的数据源。
    """
    try:
        cost_yuan = _estimate_cost_yuan(usage)
        _inc_cost_metrics(model, usage, cost_yuan)
    except Exception:
        pass  # 指标旁路失败不影响业务
    try:
        f = config.REPORTS_DIR / "cost_usage.jsonl"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": model, "tag": tag,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "reasoning_tokens": usage.get("reasoning_tokens", 0),
                "total_tokens": usage["total_tokens"],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 记录失败不影响业务
