"""Prompt 设计 + 上下文拼装（W18 Day4，溯源问答的生成侧）。

从 real_provider._build_messages 抽离并升级：
1. ★ 按 doc_id 去重：候选池 Top-N 块先按 doc 聚合并做文档级去重
2. ★ 按 QA 类型动态裁剪（W18 补全量时省 token 优化）：
   - single 类：只喂 top-3 篇 × 每篇 1 块（答案只需 1 篇，3 篇留检索余量）
   - cross/conflict 类：喂 top-5 篇 × 每篇 2 块（多文档聚合需要更多上下文）
   single 类上下文 token 减少 ~60%
3. 截断：CHUNK_CAP=800（Day1 已实证答案核心在单块内，800 足够）
4. 生成要求：必须引用 doc_id / 只依据上下文 / 冲突指出差异 / 不知道就明说
"""
import re

# 每篇最多进入上下文的块数（doc 去重后）
MAX_CHUNKS_PER_DOC = 2
# 最终进入上下文的文档数
MAX_DOCS = 5
# 单块文本截断长度（省 token：答案核心在单块内已实证，800 足够）
CHUNK_CAP = 800

# QA 类型 → 上下文规模（省 token 核心：single 类答案只需 1 篇文档）
QA_TYPE_CONTEXT = {
    "single": {"max_docs": 3, "chunks_per_doc": 1},
    "cross": {"max_docs": 5, "chunks_per_doc": 2},
    "conflict": {"max_docs": 5, "chunks_per_doc": 2},
}
DEFAULT_QA_TYPE = "single"

# 块文本中的噪音前缀（markdown 标题等），压缩以减少 token
_NOISE_RE = re.compile(r"#{1,6}\s*")


def _clean(text: str) -> str:
    return _NOISE_RE.sub("", text).strip()[:CHUNK_CAP]


def build_rag_context(hits: list[dict], max_docs: int = MAX_DOCS,
                      chunks_per_doc: int = MAX_CHUNKS_PER_DOC) -> str:
    """检索结果 → 文档级去重后的上下文文本。

    hits: [{doc_id, section_path, text, ...}]（可能同一文档多块）
    策略：
    - 按 doc_id 聚合，每篇取最多 chunks_per_doc 个块（保留块序）
    - 按文档在 hits 中首次出现的顺序排列（检索分序）
    - 每篇标题标注 doc_id + section_path，便于 LLM 引用溯源
    """
    doc_count: dict[str, int] = {}   # doc_id -> 已入选块数
    order: list[str] = []            # 文档出现顺序
    out_blocks: list[dict] = []

    for h in hits:
        doc_id = h["doc_id"]
        if doc_id not in doc_count:
            doc_count[doc_id] = 0
            order.append(doc_id)
        if doc_count[doc_id] >= chunks_per_doc:
            continue                       # 该文档块数已满
        out_blocks.append(h)
        doc_count[doc_id] += 1
        # 停止条件：已收集够 max_docs 篇，且当前篇配额已满
        if len(order) >= max_docs and doc_count[doc_id] >= chunks_per_doc:
            break

    parts = []
    for h in out_blocks:
        doc_id = h["doc_id"]
        section = h.get("section_path", "")
        text = _clean(h["text"])
        parts.append(f"[文档 {doc_id} | 章节 {section}]\n{text}")
    return "\n\n".join(parts)


def build_rag_context_for_type(hits: list[dict], qa_type: str | None) -> str:
    """按 QA 类型拼装上下文（省 token 入口）。

    single 类：3 篇 × 1 块；cross/conflict：5 篇 × 2 块。
    qa_type 未知时默认 single（保守省 token）。
    """
    cfg = QA_TYPE_CONTEXT.get(qa_type or "", QA_TYPE_CONTEXT[DEFAULT_QA_TYPE])
    return build_rag_context(hits, max_docs=cfg["max_docs"], chunks_per_doc=cfg["chunks_per_doc"])


def build_system_prompt(ctx_text: str) -> str:
    """系统提示：只依据上下文、必须引用、冲突消解、不知道就明说。"""
    return (
        "你是供应链制度问答助手。只依据提供的文档回答，不要编造；"
        "回答必须引用相关文档（citations 数组，元素为 doc_id，格式如 SCM-PUR-001_采购申请与审批管理规范）；"
        "多文档口径不同时指出差异并说明以哪份为准；检索不到就明确说不知道。\n"
        "请以 JSON 对象格式输出，字段为 {\"answer\": \"你的回答\", \"citations\": [\"doc_id1\", ...]}。\n\n"
        f"可用文档：\n{ctx_text}"
    )
