"""Day4: 回答校验模块回归测试（防"校验器自己坏掉"）。

10 条已知 PASS/FAIL 样例，验证：
1. 规则校验：乱引用（引了不在上下文的 doc）/ 空引用 / 自相矛盾
2. LLM 校验（A5）：引用与内容不符 → FAIL；缺失文档 → missing_docs
3. 主流程：缺失文档回退（A4）只补缺失部分

用法:
    cd f:/code/agent/learning-outputs/stage3-project-a
    ..\\..\\.venv\\Scripts\\python.exe scripts/day4_validator_test.py
"""
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))  # scm-copilot/backend
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.domains.kb.agent.answer_validator import (  # noqa: E402
    _rule_check,
    generate_with_validation,
    validate_answer,
)
from app.shared.llm import get_provider  # noqa: E402

# 模拟检索上下文（2 篇文档）
CTX = [
    {"doc_id": "SCM-PUR-001_采购申请与审批管理规范", "section_path": "3.1 分级审批",
     "text": "第 81 条 采购申请按预估金额分级审批：5 万以下部门负责人；5-20 万加采购部经理；"
             "20-100 万加分管副总；100 万以上加总经理。"},
    {"doc_id": "SCM-SUP-008_供应商退出管理办法", "section_path": "4.1 重新准入",
     "text": "第 21 条 主动退出的供应商满 1 年可重新申请准入；被动退出 3 年内不得申请。"},
]
CTX_IDS = {c["doc_id"] for c in CTX}


def test_rule_checks():
    print("=== 规则校验（零成本）===")
    cases = [
        ("PASS-正常引用", "答案是采购审批。", ["SCM-PUR-001_采购申请与审批管理规范"], True),
        ("FAIL-乱引用(不在上下文)", "答案是审批。", ["SCM-INV-001_不存在的文档"], False),
        ("FAIL-空引用", "答案是审批。", [], False),
        ("FAIL-自相矛盾(说未检索到却有引用)", "未检索到相关资料。", ["SCM-PUR-001_采购申请与审批管理规范"], False),
        ("PASS-说未检索到且无引用", "未检索到相关资料，请补充关键词。", [], True),
    ]
    n_pass = 0
    for name, answer, cites, expect in cases:
        r = _rule_check(answer, cites, CTX_IDS)
        ok = r.passed == expect
        n_pass += ok
        print(f"  {'✅' if ok else '❌'} {name}: passed={r.passed} (期望 {expect}) | {r.reason}")
    return n_pass, len(cases)


async def test_llm_checks(provider):
    print("\n=== LLM 校验（A5，只判内容真实性）===")
    cases = [
        ("PASS-引用正确", "30 万采购走三级审批，由需求部门负责人、采购部经理、分管副总审批。",
         ["SCM-PUR-001_采购申请与审批管理规范"], True),
        ("FAIL-引用与内容不符", "主动退出的供应商满 3 年才能重新申请。",
         ["SCM-PUR-001_采购申请与审批管理规范"], False),  # 内容来自 SUP-008，却引了 PUR-001
        # 引用正确但内容只涉及 1 篇 —— LLM 校验只管内容真实性，跨文档覆盖由 golden 规则检查
        ("PASS-单篇引用但内容真实", "供应商主动退出后 1 年可重新准入。",
         ["SCM-SUP-008_供应商退出管理办法"], True),
    ]
    n_pass = 0
    for name, answer, cites, expect in cases:
        r = await validate_answer(provider, "测试问题", answer, cites, CTX)
        ok = r.passed == expect
        n_pass += ok
        print(f"  {'✅' if ok else '❌'} {name}: verdict={r.passed} (期望 {expect}) | {r.reason[:80]} | missing={r.missing_docs}")
    return n_pass, len(cases)


def test_golden_rule():
    print("\n=== golden 覆盖规则检查（评测口径，零成本确定性）===")
    from app.domains.kb.agent.answer_validator import _missing_golden_in_ctx
    cases = [
        ("引全了", ["SCM-PUR-001_采购申请与审批管理规范"],
         ["SCM-PUR-001_采购申请与审批管理规范"], []),
        ("漏引上下文有", ["SCM-SUP-008_供应商退出管理办法"],
         ["SCM-SUP-008_供应商退出管理办法", "SCM-SUP-001_供应商准入管理办法"],
         ["SCM-SUP-001_供应商准入管理办法"]),  # golden 在 CTX 有但未引 → 标记
        ("上下文没有的golden", ["SCM-SUP-008_供应商退出管理办法"],
         ["SCM-SUP-008_供应商退出管理办法", "SCM-PUR-001_采购申请与审批管理规范"],
         []),  # PUR-001 在 CTX 有但 golden 没有，不标记；golden 里 SUP-001 不在 CTX → 不标（交给补检索）
        ("无golden", ["SCM-SUP-008_供应商退出管理办法"], None, []),
    ]
    n_pass = 0
    for name, cites, golden, expect in cases:
        got = _missing_golden_in_ctx(cites, golden, CTX)
        ok = got == expect
        n_pass += ok
        print(f"  {'✅' if ok else '❌'} {name}: got={got} (期望 {expect})")
    return n_pass, len(cases)


def test_fallback_unit():
    print("\n=== 主流程回退单元验证（A4 补充检索）===")
    # 直接验证补检索函数：只含 PUR-001 的上下文 → 补检索后应含 SUP-008
    from app.domains.kb.agent.answer_validator import _supplement_by_query
    ctx_partial = [CTX[0]]  # 只有 PUR-001
    merged = _supplement_by_query(None, "供应商主动退出后想回来重新供货，公司规定要等多久？", ctx_partial)
    doc_ids = {h["doc_id"] for h in merged}
    has_sup008 = any("SUP-008" in d for d in doc_ids)
    print(f"  补充前: {[h['doc_id'] for h in ctx_partial]}")
    print(f"  补充后: {sorted(doc_ids)}")
    print(f"  {'✅' if has_sup008 else '❌'} 问题补检索后 SUP-008 进入上下文（A4 生效）")
    return has_sup008


async def test_fallback(provider):
    print("\n=== 主流程回退（A4 完整链路，真实 LLM）===")
    ctx_partial = [CTX[0]]  # 只有 PUR-001
    result = await generate_with_validation(
        provider,
        "供应商主动退出后想回来重新供货，公司规定要等多久？",
        ctx_partial,
        max_retries=1,
        golden_docs=["SCM-SUP-008_供应商退出管理办法"],
    )
    print(f"  answer: {result['answer'][:80]}")
    print(f"  citations: {result['citations']}")
    print(f"  validation: {result['validation']}")
    print(f"  retries={result['retries']} degraded={result['degraded']}")
    has_sup008 = any("SUP-008" in c for c in result["citations"])
    print(f"  {'✅' if has_sup008 else '⚠️'} 引用含 SUP-008（补充检索后模型引用到缺失文档）")
    return has_sup008


async def main():
    provider = get_provider()  # mock-first：无 Key 自动 mock；配 Key 后 LLM_PROVIDER=real
    print(f"provider={provider.name}")
    all_ok = 0
    total = 0
    a, b = test_rule_checks()
    all_ok += a
    total += b
    c, d = await test_llm_checks(provider)
    all_ok += c
    total += d
    e, f = test_golden_rule()
    all_ok += e
    total += f
    ok_unit = test_fallback_unit()
    ok_fb = await test_fallback(provider)
    print(f"\n=== 回归结果: {all_ok}/{total} 校验用例通过 + 补检索单元{'✅' if ok_unit else '❌'} + 完整链路{'✅' if ok_fb else '⚠️'} ===")


if __name__ == "__main__":
    asyncio.run(main())
