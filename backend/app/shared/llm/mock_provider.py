"""MockLLMProvider：基于检索上下文的确定性生成。

设计（阶段三原则：mock 不降标准）：
- 不调用真实 LLM，但完整走"上下文 → 回答 → 引用"链路
- 让评测能在无 Key 时测引用准确率（W18 换 real 后同一评测集对比）
- 冲突检测：同一主题不同 doc 出现不同数字口径 → 引用两篇并标注差异
  （冲突类评测题的判定基准："引用两篇 + 以 X 为准"）
"""
import json
import re

from .base import LLMProvider

# 冲突检测：同一 doc_id 前缀（主题）内出现不同数字口径
_NUM_RE = re.compile(r"\d+(?:\.\d+)?(?:%|万元|元|天|个工作日|家|条|级|档)?")


def _head(text: str, n: int = 120) -> str:
    return " ".join(text.strip().split())[:n]


class MockLLMProvider(LLMProvider):
    name = "mock"

    def _answer_from_context(self, context: list[dict]) -> dict:
        """context: [{"doc_id", "section_path", "text"}] 检索结果

        策略：
        - 空上下文 → 明确"未检索到"，不编造
        - 单一来源 → 取该 doc 要点
        - 多来源 → 综合回答 + 引用全部
        - 含冲突（同主题不同 doc 数字不同）→ 引用两篇并标注差异
        返回 {"answer": str, "citations": [doc_id, ...]}"""
        if not context:
            return {"answer": "未检索到相关资料，请补充关键词。", "citations": []}

        docs = list({c["doc_id"] for c in context})

        # 冲突检测：同一主题前缀下，至少 2 个不同 doc_id 都含数字，且数字口径集合不同
        # （同一文档内表格多行数字属正常分布，不算冲突；跨文档数字口径差异才算）
        by_prefix: dict[str, dict[str, set[str]]] = {}
        for c in context:
            prefix = c["doc_id"].split("-")[1] if "-" in c["doc_id"] else c["doc_id"]
            nums = set(_NUM_RE.findall(c["text"]))
            if nums:
                by_prefix.setdefault(prefix, {}).setdefault(c["doc_id"], set()).update(nums)

        conflicts = []
        for prefix, doc_nums in by_prefix.items():
            doc_ids = list(doc_nums.keys())
            if len(doc_ids) < 2:
                continue
            # 冲突判定：两篇数字集合"有交集"（同事项才会数字重叠）但"口径不完全一致"
            # ——完全不相交是不同事项（如账期 vs 应付账款），不算冲突；
            #   完全一致是同口径，也不算冲突。
            base = doc_nums[doc_ids[0]]
            other = doc_nums[doc_ids[1]]
            shared = base & other
            if shared and shared != base | other:
                conflicts.append((prefix, doc_ids))

        head = _head(next(c["text"] for c in context))

        if conflicts:
            parts: list[str] = []
            for _prefix, doc_ids in conflicts[:1]:
                for c in context:
                    if c["doc_id"] in doc_ids and len(parts) < 2:
                        parts.append(f"《{c['doc_id']}》载：{_head(c['text'], 80)}")
            conflict_tip = "；".join(parts)
            answer = (f"检测到多份文件对同一事项口径不同：{conflict_tip}。"
                      f"以规则最严/最新生效者为准，具体以各文件原文为准。")
        elif len(docs) > 1:
            answer = (f"根据 {len(docs)} 份资料综合：{head}…… "
                      f"（涉及多份文档，细节见引文）")
        else:
            answer = f"根据《{docs[0]}》：{head}……"

        return {"answer": answer, "citations": docs[:5]}

    async def generate(self, messages, **kw):
        ctx = kw.get("retrieval_context", [])
        return self._answer_from_context(ctx)["answer"]

    def stream(self, messages, **kw):
        async def gen():
            r = self._answer_from_context(kw.get("retrieval_context", []))
            for ch in r["answer"]:
                yield ch  # 逐字产出，模拟真实流式
        return gen()

    async def generate_json(self, messages, schema, **kw):
        r = self._answer_from_context(kw.get("retrieval_context", []))
        # 契约：返回 {"answer", "citations"}，与 schema 解耦（schema 校验留 W18 real 后统一）
        return {"answer": r["answer"], "citations": r["citations"]}
