"""查询改写（W21 Day1）：检索前的查询扩展三方案——rewrite / multi_query / hyde。

为什么（面试 46 题素材）：
- 用户问题口语化、缺上下文（"这个申请单还有效吗"→ 不知道"这个"指什么）→ 检索向量偏
- 三个方案各补一类短板：
  1. rewrite：LLM 把短问题改写为检索友好长句（补隐含业务上下文）
  2. multi_query：拆成 2-3 个互补子查询，结果 RRF 融合（多角度覆盖，W18 教训：候选去重）
  3. hyde：先让 LLM 写假设性回答，再向量化检索（HyDE——让查询更接近文档分布）

关键设计：
- 复用 LLMProvider 抽象（mock|real 同接口）。mock 时三方案退化为规则近似
  （rewrite/hyde = 原查询，multi_query = 单元素），保证无 Key 也能跑通链路，
  Δ 数字以 real 为准（手册坑提示：改写本质是 LLM 能力）。
- HyDE 的假设回答只用于检索，不用于回答（手册坑：防幻觉）。
- multi_query 融合复用 RRF（1/(k+rank)，k=60，与 HybridRetriever 同款）。

接口：
    QueryRewriter(provider=None)          # provider 默认 get_provider()
    .is_llm : bool                         # provider.name != "mock"
    .expand(query, mode) -> list[str]      # mode: rewrite(1) | multi_query(n) | hyde(1)
    rrf_fuse_docs(doc_lists, top_k=5) -> list[str]  # doc 级 RRF 融合
"""
import json
import re

from app.shared.llm import get_provider

RRF_K = 60  # 与 HybridRetriever 同款常数

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def rrf_fuse_docs(doc_lists: list[list[str]], top_k: int = 5, k: int = RRF_K) -> list[str]:
    """doc 级 RRF 融合：score = Σ 1/(k + rank)。doc_lists: 每路子查询的 doc_id 列表。
    排名降序 + doc_id 升序（确定性 tie-breaker，可复现）。"""
    scores: dict[str, float] = {}
    for lst in doc_lists:
        for rank, doc in enumerate(lst):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank + 1)
    return [d for d, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]]


class QueryRewriter:
    """查询改写器：LLM 三方案 + mock 规则兜底。"""

    def __init__(self, provider=None):
        self.provider = provider or get_provider()

    @property
    def is_llm(self) -> bool:
        return self.provider.name != "mock"

    # ---------- 三个方案 ----------

    async def rewrite(self, query: str) -> str:
        """改写为检索友好长句（mock：原查询）。"""
        if not self.is_llm:
            return query
        prompt = (
            "你是供应链制度文档检索助手。请把下面的用户问题改写为更适合文档检索的长句查询。"
            "要求：1) 保留关键数字、金额、条款号、专有名词；2) 补充口语里被省略的业务上下文"
            "（如\"这个申请\"→\"采购申请\"）；3) 只输出改写后的查询文本本身，不要解释、不要加引号。\n"
            f"问题：{query}"
        )
        text = (await self.provider.generate(
            [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=150)).strip()
        return text or query

    async def multi_query(self, query: str) -> list[str]:
        """拆成 2-3 个互补子查询（mock：单元素 [原查询]，效果=基线，诚实标注）。"""
        if not self.is_llm:
            return [query]
        prompt = (
            "你是供应链制度文档检索助手。请把下面的用户问题拆分成 2-3 个互补的子查询，"
            "每个子查询从不同角度覆盖同一问题的检索需求。要求：子查询之间不重复、语义互补；"
            "包含关键数字、金额、条款号、专有名词；不要含序号。"
            "以 JSON 数组字符串输出，例如 [\"子查询一\", \"子查询二\", \"子查询三\"]。\n"
            f"问题：{query}"
        )
        text = (await self.provider.generate(
            [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=200)).strip()
        m = _JSON_ARRAY_RE.search(text)
        if m:
            try:
                subs = json.loads(m.group(0))
                subs = [s for s in subs if isinstance(s, str) and s.strip()]
                if subs:
                    return subs[:3]
            except json.JSONDecodeError:
                pass
        # 解析失败退化为按逗号/顿号拆行 + 原查询兜底
        parts = [p.strip() for p in re.split(r"[，,、。]", text) if 2 <= len(p.strip()) <= 60]
        return [text] if not parts else parts[:3]

    async def hyde(self, query: str) -> str:
        """先写假设性回答再用于检索（mock：退化返回原查询——无 LLM 无假设回答）。"""
        if not self.is_llm:
            return query
        prompt = (
            "你是供应链管理专家。请针对下面这个问题，写一段假设性的制度条文回答"
            "（不超过 150 字）。这段回答将用于向量检索匹配相似文档，所以请使用与制度条文"
            "一致的业务术语，尽量包含关键数字、金额、条款号、时限等具体信息。"
            "只输出回答正文，不要解释。\n"
            f"问题：{query}"
        )
        text = (await self.provider.generate(
            [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=250)).strip()
        return text or query

    # ---------- 统一入口 ----------

    async def expand(self, query: str, mode: str) -> list[str]:
        """按方案展开查询：rewrite → [1 条]；multi_query → [2-3 条]；hyde → [1 条]。"""
        mode = (mode or "rewrite").lower()
        if mode == "rewrite":
            return [await self.rewrite(query)]
        if mode == "multi_query":
            return await self.multi_query(query)
        if mode == "hyde":
            return [await self.hyde(query)]
        raise ValueError(f"未知改写模式: {mode}（可选 rewrite/multi_query/hyde）")
