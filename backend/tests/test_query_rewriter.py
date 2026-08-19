"""W27 Day5 覆盖率冲刺 I：查询改写器（query_rewriter.py）。

覆盖手册 Day5：
- mock 路径：rewrite/hyde 原样返回、multi_query 单元素 [原查询]
- LLM 路径：multi_query JSON 数组拆分、解析失败按标点退化、原查询兜底、≤3 条截断
- expand 分派与未知模式报错；is_llm 分支判定
- rrf_fuse_docs：分数累计 + 确定性 tie-breaker（doc_id 升序）+ top_k
"""
import json

import pytest

from app.shared.rag.query_rewriter import QueryRewriter, rrf_fuse_docs


class FakeLLMProvider:
    """name != "mock"，generate 返回预设文本（模拟 LLM 输出）。"""

    name = "fake"

    def __init__(self, answer: str = "默认回答"):
        self.answer = answer

    async def generate(self, messages, **kw):
        return self.answer


class TestRrfFuseDocs:
    def test_single_list_preserves_order(self):
        assert rrf_fuse_docs([["a", "b", "c"]]) == ["a", "b", "c"]

    def test_fuse_scores_accumulate(self):
        # doc "a"：列表1 rank0（1/61）+ 列表2 rank1（1/62）→ 总分最高
        out = rrf_fuse_docs([["a", "b"], ["b", "a"]], top_k=2)
        assert out == ["a", "b"]

    def test_tie_breaker_doc_id_asc(self):
        # 两 doc 累计同分 → doc_id 升序（确定性可复现）
        out = rrf_fuse_docs([["b", "a"], ["a", "b"]], top_k=5)
        assert out == ["a", "b"]

    def test_top_k_truncates(self):
        assert rrf_fuse_docs([["a", "b", "c", "d"]], top_k=2) == ["a", "b"]


class TestQueryRewriterMockPath:
    @pytest.fixture
    def mock_rw(self):
        from app.shared.llm.mock_provider import MockLLMProvider

        return QueryRewriter(provider=MockLLMProvider())

    def test_is_llm_false(self, mock_rw):
        assert mock_rw.is_llm is False

    async def test_rewrite_returns_query(self, mock_rw):
        assert await mock_rw.rewrite("采购金额多少要招标") == "采购金额多少要招标"

    async def test_multi_query_single_element(self, mock_rw):
        assert await mock_rw.multi_query("采购审批几级") == ["采购审批几级"]

    async def test_hyde_returns_query(self, mock_rw):
        assert await mock_rw.hyde("库存盘点周期") == "库存盘点周期"

    async def test_expand_modes(self, mock_rw):
        assert await mock_rw.expand("q", "rewrite") == ["q"]
        assert await mock_rw.expand("q", "multi_query") == ["q"]
        assert await mock_rw.expand("q", "hyde") == ["q"]

    async def test_expand_unknown_mode_raises(self, mock_rw):
        with pytest.raises(ValueError):
            await mock_rw.expand("q", "bogus")


class TestQueryRewriterLlmPath:
    def test_is_llm_true(self):
        assert QueryRewriter(provider=FakeLLMProvider()).is_llm is True

    async def test_multi_query_parses_json_array(self):
        ans = json.dumps(["子查询一", "子查询二", "子查询三"], ensure_ascii=False)
        rw = QueryRewriter(provider=FakeLLMProvider(ans))
        assert await rw.multi_query("问题") == ["子查询一", "子查询二", "子查询三"]

    async def test_multi_query_fallback_split(self):
        # 无 JSON 数组 → 按标点拆分（2<=len<=60 的片段）
        rw = QueryRewriter(provider=FakeLLMProvider("这是第一个拆分,这是第二个拆分"))
        assert await rw.multi_query("问题") == ["这是第一个拆分", "这是第二个拆分"]

    async def test_multi_query_fallback_single_text(self):
        rw = QueryRewriter(provider=FakeLLMProvider("仅此一句"))
        assert await rw.multi_query("问题") == ["仅此一句"]

    async def test_multi_query_filters_non_strings(self):
        rw = QueryRewriter(provider=FakeLLMProvider('["有效", 123, "  "]'))
        assert await rw.multi_query("问题") == ["有效"]

    async def test_multi_query_caps_at_three(self):
        rw = QueryRewriter(provider=FakeLLMProvider('["a", "b", "c", "d"]'))
        assert len(await rw.multi_query("问题")) == 3

    async def test_rewrite_uses_llm_output(self):
        rw = QueryRewriter(provider=FakeLLMProvider("改写后的检索长句"))
        assert await rw.rewrite("短问题") == "改写后的检索长句"

    async def test_rewrite_empty_falls_back_to_query(self):
        rw = QueryRewriter(provider=FakeLLMProvider("   "))
        assert await rw.rewrite("原问题") == "原问题"

    async def test_hyde_uses_llm_output(self):
        rw = QueryRewriter(provider=FakeLLMProvider("假设性回答"))
        assert await rw.hyde("问题") == "假设性回答"
