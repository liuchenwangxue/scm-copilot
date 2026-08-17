"""LLM 生成侧提示词（W23 Day4 从 stage3-a `agent/prompts.py` 抽入共享层）。

设计：real_provider 依赖这两个函数做"兜底 RAG 上下文拼装 + 系统提示"。
迁移后放在 shared/llm 内，shared 不跨域 import（ADR-01 边界纪律）。
"""

# ---- RAG 兜底拼装（real_provider 无 system_prompt_override 时的 fallback）----


def build_rag_context(ctx: list[dict]) -> str:
    """检索上下文 → 文本（real_provider 无 system_prompt_override 时的兜底）。"""
    parts = []
    for h in ctx:
        doc = h.get("doc_id", "")
        section = h.get("section_path", "")
        text = (h.get("text") or "")[:800]
        parts.append(f"[文档 {doc} | 章节 {section}]\n{text}")
    return "\n\n".join(parts)


def build_system_prompt(ctx_text: str) -> str:
    """系统提示：只依据上下文 + 引用 doc_id + 不知道就明说（与 stage3 项目 A 一致）。"""
    return (
        "你是供应链制度问答助手。只依据提供的文档回答，不要编造；"
        "回答必须引用相关文档（doc_id）；检索不到就明确说不知道。\n\n"
        f"可用文档：\n{ctx_text}"
    )
