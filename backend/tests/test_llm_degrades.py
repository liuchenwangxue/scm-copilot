"""W26 Day2 演练四修复的回归测试：LLM real 失败 → mock 兜底（守契约）。

覆盖：
1. generate_json 降级返回 dict（{"answer","citations","degraded":True}），
   ——修复前返回 str 会破坏 JSON 契约
2. generate 降级返回 [WARNING] 前缀 str
3. usage 记账不重复：失败模型不累计（此处验证降级路径只走一次 mock）

★ CI 可跑（无真实 Key）：monkeypatch _post_chat 抛异常模拟全模型失败。
"""
import pytest

from app.shared.llm.real_provider import RealLLMProvider


@pytest.fixture
def real_provider(monkeypatch):
    """构造 RealLLMProvider，绕过 __init__ 的 Key 检查 + mock 全失败。"""
    from app.shared.llm.mock_provider import MockLLMProvider

    p = RealLLMProvider.__new__(RealLLMProvider)
    p.models = ["glm-5.2", "deepseek-chat", "invalid-model-x"]
    p.model = "glm-5.2"  # payload 构造读取 self.model
    p.degrade_to_mock = True
    p._mock = MockLLMProvider()

    async def _boom(self, *a, **kw):
        raise RuntimeError("HTTP 401: Incorrect API key (all models failed)")

    monkeypatch.setattr(RealLLMProvider, "_post_chat", _boom)
    return p


@pytest.mark.asyncio
async def test_generate_json_degrade_returns_dict(real_provider):
    """generate_json 降级必须返回 dict（守住 JSON 契约），且带 degraded 标记。"""
    js = await real_provider.generate_json(
        [{"role": "user", "content": "供应商准入需要哪些资质材料？"}],
        schema={},
        max_tokens=256,
    )
    assert isinstance(js, dict), f"降级应返回 dict，实际 {type(js).__name__}"
    assert "answer" in js and "citations" in js
    assert js.get("degraded") is True
    assert js.get("degrade_reason")


@pytest.mark.asyncio
async def test_generate_degrade_returns_warning_str(real_provider):
    """generate 降级返回 [WARNING] 前缀 mock 文本（明确告知降级）。"""
    out = await real_provider.generate(
        [{"role": "user", "content": "采购制度中招标金额门槛是多少？"}],
        max_tokens=128,
    )
    assert isinstance(out, str)
    assert out.startswith("[WARNING]"), "降级应带 [WARNING] 标记"


@pytest.mark.asyncio
async def test_degrade_usage_not_double_counted(real_provider, monkeypatch):
    """usage 记账不重复：失败模型不累计，仅 mock 兜底成功记一次。

    验证方式：_post_chat 抛异常（所有模型失败）→ 降级路径应只调用一次
    mock.generate（不因模型池 3 个模型各记一次 usage）。
    """
    calls = {"n": 0}

    class _CountingMock:
        async def generate_json(self, msgs, schema, **kw):
            calls["n"] += 1
            return {"answer": "mock answer", "citations": []}

    real_provider._mock = _CountingMock()
    await real_provider.generate_json(
        [{"role": "user", "content": "问题"}], schema={}, max_tokens=100
    )
    assert calls["n"] == 1, "降级路径应只调用一次 mock，不重复记账"
