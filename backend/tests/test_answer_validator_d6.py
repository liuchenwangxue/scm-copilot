"""★ W28-D6 answer_validator 覆盖率收尾（B4 终验）：7% → 高覆盖。

answer_validator.py 是纯逻辑 + provider 接口（generate_json），不依赖网络——
用可编程 FakeProvider 全覆盖：
- normalize_citations：全名/短名/乱格式/去重保序
- _rule_check：诚实拒答 PASS / 无引用 FAIL / 乱引用 FAIL / 自相矛盾 FAIL
- _llm_check：PASS / FAIL + missing_docs / 异常不阻塞
- reflect / _is_low_score_candidate：低分触发 / 高分跳过 / force / 异常 fail-open
- validate_answer / generate_with_validation：全流程（含 CRAG、补检索、诚实降级）
- _missing_golden_in_ctx / _is_refusal / _supplement_by_query / _supplement_docs
"""

import pytest

from app.domains.kb.agent.answer_validator import (
    ReflectionResult,
    ValidationResult,
    _is_low_score_candidate,
    _is_refusal,
    _missing_golden_in_ctx,
    _rule_check,
    generate_with_validation,
    normalize_citations,
    reflect,
    validate_answer,
)


class FakeProvider:
    """可编程 provider：按脚本返回 generate_json 结果。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0

    async def generate_json(self, messages, schema, **kw):
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"answer": "默认回答", "citations": []}


# ==================== normalize_citations ====================


def test_normalize_citations_full_and_short():
    known = {"SCM-PUR-004_采购合同管理规范", "SCM-INV-001_库存管理"}
    out = normalize_citations(["SCM-PUR-004", "SCM-INV-001_库存管理", ""], known)
    assert out == ["SCM-PUR-004_采购合同管理规范", "SCM-INV-001_库存管理"]


def test_normalize_citations_ambiguous_keeps_original():
    """短名命中多个 → 原样保留（交给规则校验判 FAIL，不瞎猜）。"""
    known = {"SCM-PUR-001_甲", "SCM-PUR-002_乙"}
    out = normalize_citations(["SCM-PUR"], known)
    assert out == ["SCM-PUR"]


def test_normalize_citations_section_suffix():
    known = {"SCM-PUR-001_采购管理"}
    out = normalize_citations(["SCM-PUR-001_采购管理_第3章"], known)
    assert out == ["SCM-PUR-001_采购管理"]


def test_normalize_citations_dedup_preserve_order():
    known = {"SCM-PUR-001_采购管理"}
    out = normalize_citations(["SCM-PUR-001", "SCM-PUR-001_采购管理"], known)
    assert out == ["SCM-PUR-001_采购管理"]


# ==================== _rule_check ====================


def test_rule_check_honest_refusal_pass():
    r = _rule_check("未检索到相关资料", [], set())
    assert r.passed is True and "诚实拒答" in r.reason


def test_rule_check_answer_without_citation_fail():
    r = _rule_check("根据规定需要审批", [], {"D1"})
    assert r.passed is False and "无任何引用" in r.reason


def test_rule_check_fabricated_citation_fail():
    r = _rule_check("根据《D9》规定", ["D9"], {"D1", "D2"})
    assert r.passed is False and "乱引用" in r.reason


def test_rule_check_contradiction_fail():
    r = _rule_check("未检索到相关资料", ["D1"], {"D1"})
    assert r.passed is False and "自相矛盾" in r.reason


def test_rule_check_ok():
    r = _rule_check("根据规定", ["D1"], {"D1"})
    assert r.passed is True


def test_validation_result_to_dict():
    v = ValidationResult(True, "ok", [])
    assert v.to_dict() == {"passed": True, "reason": "ok", "missing_docs": []}


# ==================== _llm_check ====================


@pytest.mark.asyncio
async def test_llm_check_pass():
    p = FakeProvider([{"verdict": "PASS", "reason": "引用准确"}])
    r = await validate_answer(p, "问题", "回答", ["D1"], [{"doc_id": "D1", "text": "t"}])
    assert r.passed is True


@pytest.mark.asyncio
async def test_llm_check_fail_with_missing():
    p = FakeProvider([{"verdict": "FAIL", "reason": "缺文档", "missing_docs": ["D9"]}])
    r = await validate_answer(p, "问题", "回答", ["D1"], [{"doc_id": "D1", "text": "t"}])
    assert r.passed is False and r.missing_docs == ["D9"]


@pytest.mark.asyncio
async def test_llm_check_exception_not_blocking():
    """LLM 校验异常 → 不阻塞按通过（规则已兜底），记录原因。"""
    p = FakeProvider([TimeoutError("llm down")])
    r = await validate_answer(p, "问题", "回答", ["D1"], [{"doc_id": "D1", "text": "t"}])
    assert r.passed is True and "异常" in r.reason


@pytest.mark.asyncio
async def test_llm_check_bad_shape():
    p = FakeProvider([{"verdict": "FAIL"}])
    r = await validate_answer(p, "q", "a", ["D1"], [{"doc_id": "D1", "text": "t"}])
    assert r.passed is False


# ==================== validate_answer（规则不过不调 LLM） ====================


@pytest.mark.asyncio
async def test_validate_answer_rule_fail_no_llm():
    p = FakeProvider()  # 无脚本：若被调用会 KeyError
    r = await validate_answer(p, "q", "回答没有引用", [], [])
    assert r.passed is False
    assert p.calls == 0  # 规则不过 → 零 LLM 调用（省 token）


# ==================== reflect / _is_low_score_candidate ====================


def test_low_score_candidate():
    assert _is_low_score_candidate([]) is True  # 空上下文 → 触发
    assert _is_low_score_candidate([{"score": 0.3}]) is True  # 低分 → 触发
    assert _is_low_score_candidate([{"score": 0.9}]) is False  # 高分 → 跳过
    assert _is_low_score_candidate([{"text": "no score"}]) is False  # 无分数不猜


@pytest.mark.asyncio
async def test_reflect_skipped_when_high_score():
    p = FakeProvider()  # 无脚本：若被调用会 KeyError
    r = await reflect(p, "q", [{"score": 0.9}])
    assert r is None  # 高分不触发 → 无 LLM 调用


@pytest.mark.asyncio
async def test_reflect_forced():
    p = FakeProvider(
        [
            {
                "sufficient": False,
                "reason": "缺华东数据",
                "missing_topics": ["华东"],
                "rewrite_query": "华东区域订单",
            }
        ]
    )
    r = await reflect(p, "q", [{"score": 0.9}], force=True)
    assert r is not None and r.sufficient is False
    assert r.missing_topics == ["华东"] and r.rewrite_query == "华东区域订单"
    assert r.to_dict()["sufficient"] is False


@pytest.mark.asyncio
async def test_reflect_exception_fail_open():
    p = FakeProvider([TimeoutError("boom")])
    r = await reflect(p, "q", [{"score": 0.3}])
    assert r is not None and r.sufficient is True and "异常" in r.reason


def test_reflection_result_to_dict():
    rr = ReflectionResult(True, "ok", ["t"], "q")
    assert rr.to_dict()["missing_topics"] == ["t"]


# ==================== generate_with_validation 全流程 ====================


@pytest.mark.asyncio
async def test_generate_with_validation_ok_first_try():
    """首轮通过：返回 answer + citations + validation.passed。"""
    p = FakeProvider(
        [
            {"answer": "正确回答", "citations": ["D1"]},  # 生成
            {"verdict": "PASS", "reason": "ok"},  # 校验（规则过 + LLM PASS）
        ]
    )
    ctx = [{"doc_id": "D1", "text": "条款1"}]
    out = await generate_with_validation(p, "问题", ctx, enable_crag=False)
    assert out["answer"] == "正确回答"
    assert out["validation"]["passed"] is True
    assert out["degraded"] is False and out["retries"] == 0


@pytest.mark.asyncio
async def test_generate_with_validation_rule_fail_degrade():
    """规则不过（无引用）→ 校验 FAIL → 重试 → 仍 FAIL → 诚实降级带警告。"""
    p = FakeProvider(
        [
            {"answer": "没有引用", "citations": []},  # 生成（无引用）
            {"answer": "再次失败", "citations": []},  # 重试
        ]
    )
    ctx = [{"doc_id": "D1", "text": "t"}]
    out = await generate_with_validation(p, "问题", ctx, enable_crag=False)
    assert out["degraded"] is True
    assert "可信度低" in out["warning"]
    assert out["retries"] >= 1


@pytest.mark.asyncio
async def test_generate_with_validation_missing_golden():
    """golden 在上下文但未被引用 → 校验 FAIL → 补检索重试。"""
    p = FakeProvider(
        [
            {"answer": "回答没引 D1", "citations": []},  # 生成（缺 golden D1）
            {"answer": "引用 D1 了", "citations": ["D1"]},  # 重试（成功）
            {"verdict": "PASS", "reason": "ok"},  # 校验 PASS
        ]
    )
    ctx = [{"doc_id": "D1", "text": "t"}]
    out = await generate_with_validation(p, "问题", ctx, golden_docs=["D1"], enable_crag=False)
    assert out["validation"]["passed"] is True
    assert out["citations"] == ["D1"]


@pytest.mark.asyncio
async def test_generate_with_validation_refusal_supplement():
    """拒答形态 + 无缺失 → 用问题补一轮检索（放大候选池）。"""
    p = FakeProvider(
        [
            # 首轮生成：拒答话术但带引用（规则校验自相矛盾 FAIL，missing 空）
            {"answer": "未检索到相关资料", "citations": ["D1"]},
            # 重试生成：正常回答
            {"answer": "补检索后回答", "citations": ["D1"]},
            # 校验 PASS
            {"verdict": "PASS", "reason": "ok"},
        ]
    )
    ctx = [{"doc_id": "D1", "text": "t"}]
    # _supplement_by_query 里 `from ...hybrid_retriever import HybridRetriever`——
    # 必须 patch 源模块属性（延迟 import 在函数体内）
    import app.shared.rag.hybrid_retriever as hr_mod

    class _NoRetriever:
        def retrieve(self, q, top_k=5):
            return [{"doc_id": "D1", "text": "t", "section_path": "", "score": 1.0}]

    orig = hr_mod.HybridRetriever
    hr_mod.HybridRetriever = _NoRetriever
    try:
        out = await generate_with_validation(p, "问题", ctx, enable_crag=False)
    finally:
        hr_mod.HybridRetriever = orig
    assert out["validation"]["passed"] is True
    assert out["degraded"] is False


@pytest.mark.asyncio
async def test_generate_with_validation_crag_reflection():
    """CRAG：低分候选触发反思 → 反思结论注入生成器 + 改写查询补检索。"""
    p = FakeProvider(
        [
            {
                "sufficient": False,
                "reason": "缺华东",
                "missing_topics": ["华东"],
                "rewrite_query": "华东订单",
            },
            {"answer": "回答", "citations": []},
            {"answer": "回答2", "citations": ["D1"]},
            {"verdict": "PASS", "reason": "ok"},
        ]
    )
    ctx = [{"doc_id": "D1", "text": "t", "score": 0.3}]
    import app.shared.rag.hybrid_retriever as hr_mod

    class _NoRetriever:
        def retrieve(self, q, top_k=5):
            return [{"doc_id": "D1", "text": "t", "section_path": "", "score": 1.0}]

    orig = hr_mod.HybridRetriever
    hr_mod.HybridRetriever = _NoRetriever
    try:
        out = await generate_with_validation(p, "问题", ctx, enable_crag=True)
    finally:
        hr_mod.HybridRetriever = orig
    assert out["reflection"] is not None
    assert out["reflection"]["sufficient"] is False


# ==================== 纯函数辅助 ====================


def test_missing_golden_in_ctx():
    ctx = [{"doc_id": "D1", "text": "t"}, {"doc_id": "D2", "text": "t"}]
    assert _missing_golden_in_ctx(["D1"], ["D1", "D2"], ctx) == ["D2"]
    assert _missing_golden_in_ctx(["D1"], None, ctx) == []  # 生产问答 None
    assert _missing_golden_in_ctx(["D1", "D2"], ["D1", "D2"], ctx) == []


def test_is_refusal():
    assert _is_refusal("未检索到相关资料") is True
    assert _is_refusal("无法回答该问题") is True
    assert _is_refusal("根据规定需要审批") is False


def test_supplement_by_query_retriever_fail_returns_original():
    """补检索 retriever 异常 → 返回原上下文（fail-open）。"""
    from app.domains.kb.agent.answer_validator import _supplement_by_query

    ctx = [{"doc_id": "D1", "text": "t"}]
    import app.shared.rag.hybrid_retriever as hr_mod

    class _BoomRetriever:
        def retrieve(self, q, top_k=5):
            raise ConnectionError("qdrant down")

    orig = hr_mod.HybridRetriever
    hr_mod.HybridRetriever = _BoomRetriever
    try:
        out = _supplement_by_query(FakeProvider(), "q", ctx)
    finally:
        hr_mod.HybridRetriever = orig
    assert out == ctx


def test_supplement_docs_missing_none():
    from app.domains.kb.agent.answer_validator import _supplement_docs

    ctx = [{"doc_id": "D1", "text": "t"}]
    out = _supplement_docs(FakeProvider(), "q", ctx, [])
    assert out == ctx  # need 空 → 直接返回


def test_supplement_docs_retriever_exception_returns_original():
    from app.domains.kb.agent.answer_validator import _supplement_docs

    ctx = [{"doc_id": "D1", "text": "t"}]
    import app.shared.rag.hybrid_retriever as hr_mod

    class _BoomRetriever:
        def retrieve(self, q, top_k=5):
            raise ConnectionError("qdrant down")

    orig = hr_mod.HybridRetriever
    hr_mod.HybridRetriever = _BoomRetriever
    try:
        out = _supplement_docs(FakeProvider(), "q", ctx, ["D9"])
    finally:
        hr_mod.HybridRetriever = orig
    assert out == ctx
