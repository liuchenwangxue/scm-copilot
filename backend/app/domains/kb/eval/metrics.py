"""评测指标（沿用 W3 定义 + 新增引用准确率）。

指标定义：
- Hit@1       : Top-1 doc_id 命中 source_doc_ids 任意一个
- Recall@5    : Top-5 与 source_doc_ids 交集非空
- 含答案率     : Top-5 拼接文本包含答案核心片段（答案前 20 字去标点）
- 引用准确率   : 对每条 QA 调 generate_json，检查 citations 是否命中 source_doc_ids
                （全部命中计 1.0，部分命中按比例计——反映"引用溯源对不对"）

失败样例：Top-1 未命中 + 引用未全部命中的样例都记录，供 W18 优化定位。
"""
import re


def core_of(answer: str) -> str:
    """答案核心片段：跳过"按《XX》第X条"式引文前缀，去句末标点取前 20 字。

    W3 同款口径，但修正一个真实问题：评测集答案习惯先写"按《文档》第X条"，
    文档原文是"第 X 条 内容"（条号前置）。若 core 含引文前缀则永远匹配不上。
    """
    text = answer.strip()
    # 去掉前导 "按《...》第...条，" / "根据《...》" 等引文引导
    text = re.sub(r"^(按|根据|依据|见)?(《[^》]+》)?(第\s*\d+(\s*-\s*\d+)?\s*条(第\s*\d+款)?[，,、]?\s*)*", "", text)
    # 逐字符去句末标点（B005：strip 多字符集合是"集合去重"语义，非按序去后缀，用显式循环）
    while text and text[-1] in "。！？；;，、：,.!?;:）\"'":
        text = text[:-1]
    return text[:20]


def answer_in_top5(top_text: str, answer: str) -> bool:
    """含答案率：Top-5 拼接文本是否包含答案核心片段（W3 同款，20 字连续子串）。

    ★ 定位说明（长文档下的诚实预期）：
    - 这是"参考答案文本是否被召回"的严格版指标（chunk 级，非 doc 级）。
    - Recall@5(0.98) 已证明"答案所在文档"几乎全被召回；但文档被召回 ≠
      "答案那一小块条文"进了 Top-5 chunk 拼接。长文档下答案片段可能排在 Top-5 外，
      故本指标偏低（~0.23）是真实反映，正是 W18 混合检索/重排的优化空间。
    - 与 W3 口径保持一致（不强行改算法），报告中与 Recall@5 对照解读。
    """
    return core_of(answer) in top_text


def citation_accuracy(citations: list[str], golden: set[str]) -> float:
    """引用准确率：golden 文档被引用的覆盖率（0~1）。
    手册定义"检查 citations 是否命中 source_doc_ids（命中即 1）"——
    用 golden 覆盖率：命中的 golden 数 / golden 总数。
    防幻觉视角：标准答案涉及的文档，回答是否都引用了。
    - single 类 golden=1 篇，引到即 1.0；跨文档 golden=2 篇，两篇都引到才 1.0。
    """
    if not golden or "<EMPTY_GOLDEN>" in golden:
        return 0.0
    hit = sum(1 for g in golden if g in citations)
    return hit / len(golden)


def evaluate_retrieval(top_docs: list[str], golden: set[str], top_text: str, answer: str) -> dict:
    """单条检索侧指标。top_docs: Top-K 去重后的 doc_id 列表（保持顺序）。"""
    is_hit1 = bool(top_docs and top_docs[0] in golden)
    is_recall5 = any(d in golden for d in top_docs)
    is_answer = answer_in_top5(top_text, answer)
    return {"hit@1": int(is_hit1), "recall@5": int(is_recall5), "answer_rate": int(is_answer)}


def aggregate_metrics(items: list[dict], total_n: int) -> dict:
    """汇总一组样本指标为总体指标。items 是含 hit@1/recall@5/answer_rate/citation_acc 的 dict 列表。"""
    n = max(len(items), 1)
    return {
        "hit@1": round(sum(i["hit@1"] for i in items) / n, 4),
        "recall@5": round(sum(i["recall@5"] for i in items) / n, 4),
        "answer_rate": round(sum(i["answer_rate"] for i in items) / n, 4),
        "citation_accuracy": round(sum(i.get("citation_acc", 0) for i in items) / n, 4),
        "n": total_n,
    }
