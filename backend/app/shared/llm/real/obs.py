"""观测：LangFuse generation span（★ W27 Day4 拆出）。

职责边界：LLM_ENABLE_LANGFUSE=1 时挂 generation span（W9 复用）；
观测失败不阻塞业务（fail-open）。全局单例 `_obs` 供 provider 使用。

依赖方向：obs → config；被 provider 引用（obs 不引用 provider，避免循环）。
"""

from __future__ import annotations

from typing import Any

from app.shared import config


class _Observability:
    """LangFuse 观测（旁路，fail-open）：LLM_ENABLE_LANGFUSE=1 时挂 generation span。"""

    def __init__(self):
        self.enabled = config.LLM_ENABLE_LANGFUSE if hasattr(config, "LLM_ENABLE_LANGFUSE") else False
        self._lf = None

    def _get(self):
        if not self.enabled:
            return None
        if self._lf is None:
            try:
                from langfuse import Langfuse
                self._lf = Langfuse(
                    public_key=config.LANGFUSE_PUBLIC_KEY,
                    secret_key=config.LANGFUSE_SECRET_KEY,
                    host=config.LANGFUSE_HOST,
                    debug=False,
                )
            except Exception:
                self.enabled = False
        return self._lf

    def generation(self, name: str, model: str, messages: list[dict], output: str | None,
                   usage: dict | None, metadata: dict | None = None):
        lf = self._get()
        if lf is None:
            return
        try:
            gen = lf.generation(
                name=name, model=model,
                input={"messages": messages[-2:]},
                output=output or "", metadata=metadata or {},
            )
            if usage:
                md = dict(metadata or {})
                md["reasoning_tokens"] = usage.get("reasoning_tokens", 0)
                gen.update(
                    usage={
                        "input": usage["prompt_tokens"],
                        "output": usage["completion_tokens"],
                        "total": usage["total_tokens"],
                    },
                    metadata=md,
                )
            gen.end()
            lf.flush()
        except Exception:
            pass  # 观测失败不影响业务


_obs = _Observability()
