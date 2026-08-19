"""real 提供方包（★ W27 Day4 拆分：real_provider.py 28.6KB → 五模块）。

模块职责：
- provider.py    generate/stream/generate_json 编排 + 降级链（组合入口）
- model_pool.py  模型池 + 切换 + llm_model_state.json 持久化
- errors.py      _ProviderError + _retryable() + 额度耗尽关键词（纯函数）
- cost.py        token/cost 记录（cost_usage.jsonl + Prometheus）
- obs.py         LangFuse 上报

公共 API：RealLLMProvider（老导入 `app.shared.llm.real_provider` 走 shim 不破）。
"""
from .provider import RealLLMProvider  # noqa: F401

__all__ = ["RealLLMProvider"]
