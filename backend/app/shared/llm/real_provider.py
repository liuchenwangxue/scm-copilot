"""RealLLMProvider：真实 LLM 接入（★ W18 中段落地，Day3 实现）。

★ W27 Day4 拆分（B3）：real_provider.py 28.6KB 职责过重（模型池+重试+cost+LangFuse+流式）
→ 拆为 `app.shared.llm.real` 包五模块（provider/model_pool/errors/cost/obs）。
本文件保留为 **兼容 shim**：老导入路径 `from app.shared.llm.real_provider import
RealLLMProvider` 继续可用（全仓既有测试/调用零改动）。

拆分原则（手册 Day4 坑）：
- 依赖方向单向：provider → model_pool → errors；cost/obs 只被 provider 引（防循环导入）
- `_retryable()` 保持纯函数（无 IO），D5 补测才便宜
- 公共 API 不动：类名 / 三接口 / `get_provider("real")` 全部不变
"""
from app.shared.llm.real import RealLLMProvider  # noqa: F401

__all__ = ["RealLLMProvider"]
