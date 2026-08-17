"""LLMProvider 抽象层：mock 与 real 同接口，环境变量 LLM_PROVIDER=mock|real 切换。

设计目标（阶段三原则 3 落地）：
- W17 用 mock 实现就能测"生成链路 + 引用准确率"（无 Key 不阻塞评测）
- W18 接真实 Key 只改 real_provider.py + factory 默认值，业务代码零改动
- 三个接口覆盖三种生产场景：
    generate      普通问答（完整文本返回）
    stream        流式输出（W15 前端 SSE 三型事件对接用）
    generate_json 结构化输出（回答 + 引用，评测/溯源用）

契约：
- messages: [{"role": "system"|"user"|"assistant", "content": str}]
- kw 透传（mock 用 retrieval_context，real 用 temperature/max_tokens 等）
- generate_json 返回 dict（符合 schema 的键值对）
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def generate(self, messages: list[dict], **kw) -> str:
        """普通生成，返回完整文本。messages: [{"role": "system"|"user"|"assistant", "content": str}]"""

    @abstractmethod
    def stream(self, messages: list[dict], **kw) -> AsyncIterator[str]:
        """流式生成，逐块产出文本（W15 前端三型事件对接用）"""

    @abstractmethod
    async def generate_json(self, messages: list[dict], schema: dict, **kw) -> dict:
        """结构化输出（回答 + 引用），schema 为 JSON Schema。返回 dict"""


def get_provider(name: str | None = None) -> LLMProvider:
    """工厂：LLM_PROVIDER=mock|real，默认 mock（W18 中段接入真实 Key 后默认切 real）"""
    import os
    n = (name or os.getenv("LLM_PROVIDER") or "mock").lower()
    if n == "mock":
        from .mock_provider import MockLLMProvider
        return MockLLMProvider()
    if n == "real":
        from .real_provider import RealLLMProvider
        return RealLLMProvider()
    raise ValueError(f"unknown provider: {n}")
