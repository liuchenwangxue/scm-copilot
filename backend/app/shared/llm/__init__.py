"""LLM 抽象层包：mock 与 real 同接口，环境变量 LLM_PROVIDER=mock|real 切换。"""
from .base import LLMProvider, get_provider  # noqa: F401
