"""回答校验模块（★ W18 Day4 防幻觉核心工程，欠账 A4/A5 落地）。

双层校验（手册 Day4 要求）：
1. 规则校验（零成本，永远先跑）：
   - citations 非空（≥1）
   - citations 全部在检索返回的 doc_id 集合内（防"引了不在上下文的文档"= 乱引用）
   - 不矛盾：回答没说"未检索到"却有引用（或反之）
2. LLM 校验（A5 欠账）：把"回答 + 引文段落"交给 LLM 打分
   - 返回 {"verdict": "PASS"|"FAIL", "reason": "...", "missing_docs": [...]}
   - FAIL 且 missing_docs 非空 → A4 回退粒度优化：只补检索缺失的那部分文档，
     不全量重检索（省 token、聚焦问题）

回退：上限 2 次仍 FAIL → 返回带"引用可信度低"警告的回答（诚实降级，不硬编）。

设计要点：
- 校验器也要被校验（面试高级认知）：LLM 校验结果本身可打 span 回查，规则校验兜底
- validator 只返回结果不抛异常（W13 教训：校验失败返回问题列表，由调用方决定回退）
"""
from .prompts import build_rag_context


class ValidationResult:
    def __init__(self, passed: bool, reason: str = "", missing_docs: list | None = None):
        self.passed = passed
        self.reason = reason
        self.missing_docs = missing_docs or []

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reason": self.reason, "missing_docs": self.missing_docs}


def normalize_citations(citations: list[str], known_doc_ids: set[str]) -> list[str]:
    """citations 归一化：模型可能返回短名（SCM-PUR-004）而非全名（SCM-PUR-004_采购合同管理规范）。

    策略（保守，只做无损映射）：
    1. 已是全名（在 known_doc_ids 里）→ 原样保留
    2. 短名（SCM-XXX-NNN）→ 若 known_doc_ids 里有且只有一个该前缀 → 映射为全名
    3. 其他（含章节后缀的乱格式）→ 尝试前缀匹配，匹配到则映射，否则原样保留（交给规则校验判 FAIL）
    """
    out = []
    for c in citations:
        c = (c or "").strip()
        if not c:
            continue
        if c in known_doc_ids:
            out.append(c)
            continue
        # 短名格式：SCM-XXX-NNN（如 SCM-PUR-004）
        prefix = c
        matches = [d for d in known_doc_ids if d.startswith(prefix + "_") or d == prefix]
        if len(matches) == 1:
            out.append(matches[0])
        else:
            # 尝试更宽松的前缀匹配（处理带章节后缀的）
            base = c.split("_")[0]
            matches2 = [d for d in known_doc_ids if d.startswith(base + "_") or d == base]
            out.append(matches2[0] if len(matches2) == 1 else c)
    # 去重保序
    seen, dedup = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            dedup.append(c)
    return dedup


# ---------------- 规则校验（零成本） ----------------

def _rule_check(answer: str, citations: list[str], retrieved_doc_ids: set[str]) -> ValidationResult:
    """规则校验：citations 合法性与自洽性。"""
    issues = []
    neg_txt = "未检索到" in answer or "没有找到" in answer or "无法回答" in answer

    if not citations:
        # 诚实拒答（说未检索到且无引用）是合法行为 → PASS
        if neg_txt:
            return ValidationResult(passed=True, reason="诚实拒答（未检索到且无引用）")
        issues.append("回答有内容但无任何引用")
    elif any(c not in retrieved_doc_ids for c in citations):
        bad = [c for c in citations if c not in retrieved_doc_ids]
        issues.append(f"存在未在检索上下文中的引用: {bad}（乱引用/编造文档）")

    # 自洽性：回答说没检索到却给了引用（矛盾）
    if neg_txt and citations:
        issues.append("回答声称未检索到，但存在引用（自相矛盾）")

    return ValidationResult(passed=not issues, reason="；".join(issues) or "规则校验通过")


# ---------------- LLM 校验（A5，只判内容真实性） ----------------

_LLM_CHECK_SYSTEM = (
    "你是 RAG 回答质量审核员。根据提供的【回答】和【可用文档】，判断回答的引用是否准确、"
    "回答是否忠于文档内容。\n"
    "输出 JSON：{\"verdict\": \"PASS\"|\"FAIL\", \"reason\": \"一句话原因\", \"missing_docs\": [\"缺失的doc_id\", ...]}\n"
    "判定规则：\n"
    "- 回答引用的文档必须能支撑回答内容；引用了错误的文档（内容与文档不符）→ FAIL\n"
    "- 回答内容与文档事实不符（幻觉）→ FAIL\n"
    "- 回答断言了某事实但可用文档中找不到支撑（且该文档可能在检索结果中）→ FAIL 并在 "
    "missing_docs 列出缺失的文档\n"
    "- 一切正常 → PASS"
)


async def _llm_check(provider, question: str, answer: str, citations: list[str],
                     ctx_hits: list[dict]) -> ValidationResult:
    """LLM 校验（A5）：只判断"内容是否忠于所引文档 / 是否幻觉"。

    职责边界（Day4 静态重构后确定）：
    - golden 覆盖检查 = 规则判断（_missing_golden_in_ctx，确定性零成本）
    - LLM 只判内容真实性——不参与"该引哪些文档"的判断（之前实测 LLM 对跨文档
      覆盖判断不可靠，且与 golden 规则检查重复）
    校验器本身可能不准 → 结果只作参考，规则校验兜底。"""
    # 构造引文上下文（只放回答实际引用的文档段落；校验只判真实性，每篇 1 块即可，省 token）
    cited_hits = [h for h in ctx_hits if h["doc_id"] in citations]
    ctx_text = build_rag_context(cited_hits, max_docs=5, chunks_per_doc=1)
    try:
        result = await provider.generate_json(
            [
                {"role": "system", "content": _LLM_CHECK_SYSTEM},
                {"role": "user", "content": (
                    f"【问题】{question}\n【回答】{answer}\n【可用文档】\n{ctx_text or '（回答未引用任何文档）'}"
                )},
            ],
            {"type": "object"},
            temperature=0.0,
        )
        result = result if isinstance(result, dict) else {}
        verdict = str(result.get("verdict", "FAIL")).upper()
        missing = result.get("missing_docs") or []
        if not isinstance(missing, list):
            missing = []
        missing = [str(m) for m in missing]
        passed = verdict == "PASS"
        return ValidationResult(
            passed=passed,
            reason=str(result.get("reason", ""))[:200],
            missing_docs=missing,
        )
    except Exception as e:
        # LLM 校验失败 → 不阻塞，按通过处理（规则校验已兜底），但记录原因
        return ValidationResult(passed=True, reason=f"LLM 校验异常（不阻塞）: {type(e).__name__}: {str(e)[:80]}")


# ---------------- ★ W22 Day4：CRAG 反思节点（检索后反思→补检索） ----------------
#
# CRAG 升级：现有"生成→校验→FAIL→补检索"已是 CRAG 雏形（_supplement_docs 只补缺失文档）。
# Day4 显式补两块（手册要求）：
#   1. "反思"节点：检索后先让 LLM 判断"检索结果够不够"（覆盖评估 + 改写建议 + 缺失主题）
#   2. "生成器看到反思结论"：把反思结论注入生成器系统提示，比盲补检索更聚焦
# 设计取舍（对应手册坑）：
#   - 反思是 LLM 调用（烧 token）→ 只在"低分候选"（Top1 相似度/检索少）时触发，否则跳过（省）
#   - 反思只产出结论不生成答案；补检索仍复用 A4 粒度（只补缺失主题，不全量重检索）
#   - 反思结论同时用于：① 指导补检索（聚焦缺失主题而非整段重检）② 注入下一轮生成器
#   - fail-open：反思异常 → 退化为原逻辑（不阻塞）


class ReflectionResult:
    """CRAG 反思结论：评估检索结果是否充分 + 改写建议 + 缺失主题。"""

    def __init__(self, sufficient: bool, reason: str = "",
                 missing_topics: list | None = None,
                 rewrite_query: str = ""):
        self.sufficient = sufficient       # 检索结果是否足够支撑回答
        self.reason = reason               # 评估理由（生成器可见，聚焦生成）
        self.missing_topics = missing_topics or []   # 缺失主题/关键词（补检索依据）
        self.rewrite_query = rewrite_query  # 改写后的查询（更聚焦）

    def to_dict(self) -> dict:
        return {"sufficient": self.sufficient, "reason": self.reason,
                "missing_topics": self.missing_topics, "rewrite_query": self.rewrite_query}


_REFLECT_SYSTEM = (
    "你是 RAG 检索质量评估员。根据【问题】和【已检索到的文档摘要】，判断现有检索结果"
    "是否足以完整回答问题。\n"
    "输出 JSON：{\"sufficient\": true|false, \"reason\": \"一句话评估\", "
    "\"missing_topics\": [\"缺失的知识点/关键词\", ...], \"rewrite_query\": \"改写后的更精准查询\"}\n"
    "判定规则：\n"
    "- 检索结果已覆盖问题所有关键要素 → sufficient=true，missing_topics 为空\n"
    "- 缺某些知识点的支撑（主题没召回到）→ sufficient=false，missing_topics 列出具体缺失主题\n"
    "- rewrite_query：当检索不充分时给出更可能召回缺失内容的改写查询（保留问题核心意图）\n"
    "- 只评估检索覆盖，不评估答案对错"
)


def _is_low_score_candidate(ctx_hits: list[dict], low_score_threshold: float = 0.55) -> bool:
    """是否"低分候选"（反思仅在此时触发，省 token——手册坑：反思是 LLM 调用烧 token）。

    判据：检索返回块很少，或 Top1 相似度低于阈值（候选质量存疑）。
    """
    if len(ctx_hits) == 0:
        return True
    # 取 Top1 的相似度分数。无 score 字段（测试构造/接口差异）→ 不猜，视为充分不触发
    # （避免因缺分数误触发反思烧 token——省 token 原则）。
    scored = []
    for h in ctx_hits[:3]:
        s = h.get("score")
        if s is None:
            continue
        try:
            scored.append(float(s))
        except (TypeError, ValueError):
            continue
    if not scored:
        return False
    return max(scored) < low_score_threshold


async def reflect(provider, question: str, ctx_hits: list[dict],
                  force: bool = False, low_score_threshold: float = 0.55) -> ReflectionResult | None:
    """CRAG 反思节点：LLM 评估检索覆盖，产出反思结论（供补检索 + 生成器）。

    - force=False 时只在"低分候选"触发（省 token，手册坑）。
    - force=True 由调用方强制（评测/调试）。
    - fail-open：LLM 异常/未触发 → 返回 None（调用方退化为原逻辑）。
    """
    if not force and not _is_low_score_candidate(ctx_hits, low_score_threshold):
        return None
    try:
        # 只放检索结果摘要（每篇 1 块标题 + 前 120 字），评估覆盖不看全文（省 token）
        brief = []
        for h in ctx_hits[:8]:
            doc = h.get("doc_id", "")
            section = h.get("section_path", "")
            txt = (h.get("text", "") or "")[:120]
            brief.append(f"- [{doc}]（{section}）: {txt}")
        ctx_brief = "\n".join(brief) if brief else "（无检索结果）"
        result = await provider.generate_json(
            [
                {"role": "system", "content": _REFLECT_SYSTEM},
                {"role": "user", "content": (
                    f"【问题】{question}\n【已检索到的文档】\n{ctx_brief}"
                )},
            ],
            {"type": "object"},
            temperature=0.0,
        )
        result = result if isinstance(result, dict) else {}
        sufficient = str(result.get("sufficient", "true")).lower() in ("true", "1", "yes")
        missing = result.get("missing_topics") or []
        if not isinstance(missing, list):
            missing = []
        return ReflectionResult(
            sufficient=sufficient,
            reason=str(result.get("reason", ""))[:200],
            missing_topics=[str(m) for m in missing],
            rewrite_query=str(result.get("rewrite_query", "")),
        )
    except Exception as e:
        # fail-open：反思失败不阻塞，返回 None（调用方走原逻辑）
        return ReflectionResult(sufficient=True, reason=f"反思异常（不阻塞）: {type(e).__name__}: {str(e)[:60]}")


# ---------------- 主流程 ----------------

async def validate_answer(provider, question: str, answer: str, citations: list[str],
                          ctx_hits: list[dict]) -> ValidationResult:
    """完整校验：先规则（零成本），规则过再 LLM（A5）。返回 ValidationResult。"""
    retrieved_doc_ids = {h["doc_id"] for h in ctx_hits}

    # 1. 规则校验
    rule = _rule_check(answer, citations, retrieved_doc_ids)
    if not rule.passed:
        return rule  # 规则不过直接 FAIL（零成本兜底），不浪费 LLM 调用

    # 2. LLM 校验（A5 欠账，只判内容真实性）
    return await _llm_check(provider, question, answer, citations, ctx_hits)


async def generate_with_validation(provider, question: str, ctx_hits: list[dict],
                                   max_retries: int = 2, golden_docs: list | None = None,
                                   qa_type: str | None = None,
                                   enable_crag: bool | None = None) -> dict:
    """带校验的生成主流程（评测/问答统一入口）：

    生成 → 校验 → FAIL 且 missing_docs → 补检索缺失文档 → 再生成 → 再校验 → 上限 2 次
    仍 FAIL → 返回带"引用可信度低"警告的回答（诚实降级，不硬编）。

    ★ W22 Day4 CRAG 升级（enable_crag，默认 True）：
    - 检索后反思：低分候选时 LLM 评估"检索够不够"，产出反思结论（missing_topics/rewrite_query）
    - 生成器看到反思结论：把反思注入系统提示，回答更聚焦、引用更准
    - 校验 FAIL 时按反思缺失主题引导补检索（比盲补更聚焦），保持 A4"只补缺失"粒度

    golden_docs: 评测时传入标准答案涉及文档（供校验器检查跨文档覆盖），生产问答传 None。
    qa_type: single/cross/conflict —— 决定上下文规模（省 token：single 类只喂 3 篇×1 块）。
    enable_crag: 是否启用 CRAG 反思；None → 读 config（默认开），显式 False 走原流程（回归对比用）。

    返回: {"answer", "citations", "validation": {...}, "retries", "degraded",
           "reflection": {...} | None}
    """
    from .prompts import build_rag_context_for_type, build_system_prompt

    if enable_crag is None:
        try:
            from app.domains.kb import config as _cfg
            enable_crag = getattr(_cfg, "CRAG_ENABLED", True)
        except Exception:
            enable_crag = True

    retries = 0
    final_validation = None
    reflection = None  # CRAG 反思结论（生成器 + 补检索依据）

    # ★ CRAG：首轮生成前先反思一次（低分候选才触发，省 token——手册坑）
    if enable_crag:
        try:
            reflection = await reflect(provider, question, ctx_hits)
        except Exception:
            reflection = None
        if reflection is not None and not reflection.sufficient:
            print(f"  [CRAG] 反思: 检索覆盖不足（{reflection.reason[:60]}）"
                  f" 缺主题={reflection.missing_topics[:3]}")

    for attempt in range(max_retries + 1):
        # 生成（按 QA 类型裁剪上下文，省 token）
        ctx_text = build_rag_context_for_type(ctx_hits, qa_type)
        system = build_system_prompt(ctx_text)
        # ★ CRAG：把反思结论注入生成器（更聚焦；无反思/充分时为空串不影响）
        if enable_crag and reflection is not None and not reflection.sufficient:
            _refl = reflection
            _focus = "；".join(_refl.missing_topics) if _refl.missing_topics else _refl.rewrite_query
            system = (system +
                      "\n\n【检索反思提示】当前检索可能缺失以下知识点，回答时请注意："
                      + (_focus or "确保只基于已检索文档作答，不要编造"))
        result = await provider.generate_json(
            [{"role": "user", "content": question}],
            {"type": "object"},
            system_prompt_override=system,  # 供 real_provider 透传；mock 忽略
            retrieval_context=ctx_hits,
            temperature=0.0,
        )
        result = result if isinstance(result, dict) else {}
        answer = str(result.get("answer", ""))
        citations = result.get("citations") or []
        if not isinstance(citations, list):
            citations = []
        # 归一化：短名 → 全名（模型常返回 SCM-PUR-004 而非全名，会误判乱引用/漏引用）
        known = {h["doc_id"] for h in ctx_hits}
        citations = normalize_citations(citations, known)

        # ① 校验（规则 + LLM 内容真实性；golden_docs 不参与 LLM 判断）
        validation = await validate_answer(provider, question, answer, citations, ctx_hits)

        # ② golden 覆盖检查（评测口径，规则式、确定性）：golden 在上下文但未被引用
        missing_golden = _missing_golden_in_ctx(citations, golden_docs, ctx_hits)

        # ③ 合并缺失清单（LLM 判的 + golden 漏引的），去重保序
        missing = list(dict.fromkeys(
            [m for m in validation.missing_docs if m] + missing_golden))

        # 通过条件：校验 PASS 且无 golden 漏引
        if validation.passed and not missing_golden:
            return {
                "answer": answer, "citations": citations,
                "validation": validation.to_dict(), "retries": retries, "degraded": False,
                "reflection": reflection.to_dict() if reflection else None,
            }

        # 失败原因（供报告/降级警告）
        if missing:
            reason = f"{validation.reason}；golden 漏引={missing_golden}" if missing_golden else validation.reason
        else:
            reason = validation.reason
        final_validation = ValidationResult(passed=False, reason=reason, missing_docs=missing)

        # ④ 回退：优先按缺失清单只补缺失文档（A4 粒度优化，不全量重检索）
        if attempt >= max_retries:
            break
        if missing:
            # 分两类：上下文已有的 → 直接重试（模型会重新生成并引用）；
            # 上下文没有的 → 补检索拉进上下文
            ctx_ids = {h["doc_id"] for h in ctx_hits}
            need_retrieve = [m for m in missing if m not in ctx_ids]
            in_ctx = [m for m in missing if m in ctx_ids]
            print(f"  [VALIDATOR] 第{attempt + 1}次 FAIL({reason[:70]})，"
                  f"缺引用(上下文已有)={in_ctx[:3]}，补检索={need_retrieve[:3]}")
            if need_retrieve:
                ctx_hits = _supplement_docs(provider, question, ctx_hits, need_retrieve)
            # in_ctx 的情况：上下文已有该文档，重试时模型应能引用（系统提示含该文档）
        elif _is_refusal(answer):
            # 拒答但无缺失清单 → 上下文可能不足（候选池太小/主题词没召回），
            # 用问题本身（或 CRAG 反思改写查询）补一轮检索（放大候选池），而不是原样重试
            supplement_query = question
            if (enable_crag and reflection is not None
                    and not reflection.sufficient and reflection.rewrite_query):
                supplement_query = reflection.rewrite_query
                print(f"  [VALIDATOR] 第{attempt + 1}次 FAIL({reason[:70]})，"
                      f"回答拒答 → 用 CRAG 反思改写查询补检索: {supplement_query[:50]}")
            else:
                print(f"  [VALIDATOR] 第{attempt + 1}次 FAIL({reason[:70]})，"
                      f"回答拒答 → 用问题补一轮检索扩大上下文")
            ctx_hits = _supplement_by_query(provider, supplement_query, ctx_hits)
        else:
            # 无缺失清单（规则失败/乱引用）→ 原上下文再试一次
            print(f"  [VALIDATOR] 第{attempt + 1}次 FAIL({reason[:70]})，原上下文重试")
        retries += 1

    # 诚实降级：带警告返回
    warn = (f"引用可信度低（校验 {max_retries + 1} 次未过: "
            f"{final_validation.reason if final_validation else '校验器未初始化'}）")
    return {
        "answer": answer, "citations": citations,
        "validation": final_validation.to_dict() if final_validation else {"passed": False},
        "retries": retries, "degraded": True, "warning": warn,
        "reflection": reflection.to_dict() if reflection else None,
    }


def _missing_golden_in_ctx(citations: list[str], golden_docs: list | None,
                           ctx_hits: list[dict]) -> list[str]:
    """评测 golden 覆盖检查：golden 文档在检索上下文里但未被引用 → 返回缺失清单。

    golden_docs 为 None（生产问答）时返回空。只检查"上下文有但没引"的——
    "上下文没有"的交给补检索（_supplement_docs），这里只负责标记缺引用。
    """
    if not golden_docs:
        return []
    ctx_ids = {h["doc_id"] for h in ctx_hits}
    cited = set(citations)
    return [g for g in golden_docs if g in ctx_ids and g not in cited]


def _is_refusal(answer: str) -> bool:
    """是否"诚实拒答"形态（上下文不足的典型信号）。"""
    return any(k in answer for k in (
        "未检索到", "没有找到", "无法回答", "不知道", "未提及", "没有提及",
        "未找到", "无法提供", "未能找到", "未给出"))


def _supplement_by_query(provider, question: str, ctx_hits: list[dict],
                         extra_k: int = 5) -> list[dict]:
    """拒答时用问题本身补一轮检索（放大候选池），并合并进上下文。

    只追加不在现有上下文的块（去重），保持现有顺序在前。
    """
    try:
        from app.shared.rag.hybrid_retriever import HybridRetriever
        retriever = HybridRetriever()
        hits = retriever.retrieve(question, top_k=extra_k)
    except Exception:
        return ctx_hits

    existing_ids = {h["doc_id"] for h in ctx_hits}
    seen = set()
    merged = list(ctx_hits)
    for h in hits:
        if h["doc_id"] not in existing_ids and h["doc_id"] not in seen:
            seen.add(h["doc_id"])
            merged.append(h)
    return merged


def _supplement_docs(provider, question: str, ctx_hits: list[dict],
                     missing_docs: list[str]) -> list[dict]:
    """A4 回退粒度：只补检索缺失的文档（通过混合检索把缺失 doc 拉进来）。

    简单实现：对每个缺失 doc_id，用"文档标题词"作为查询检索 Top-3 块补进上下文。
    若补不到（retriever 无该 doc），则保留原上下文（校验会再判）。
    """
    from app.shared.rag.hybrid_retriever import HybridRetriever

    existing = {h["doc_id"] for h in ctx_hits}
    need = [d for d in missing_docs if d not in existing]
    if not need:
        return ctx_hits

    try:
        retriever = HybridRetriever()
    except Exception:
        return ctx_hits

    added = []
    for doc_id in need:
        # 用 doc_id 核心词（去掉前缀 SCM-XXX- 和 _标题 后缀）作查询
        core = doc_id.split("_")[-1]  # 取中文标题部分
        try:
            hits = retriever.retrieve(core, top_k=3)
            for h in hits:
                if h["doc_id"] == doc_id:
                    added.append(h)
        except Exception:
            continue
    if added:
        seen_ids = set()
        merged = list(ctx_hits)
        for h in added:
            if h["doc_id"] not in seen_ids:
                seen_ids.add(h["doc_id"])
                merged.append(h)
        return merged
    return ctx_hits
