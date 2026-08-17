"""重排器：Top-20 候选 → Top-5 精排（W18 Day2）。

两级实现（手册允许规则降级，诚实标注"规则版"）：
1. RuleReranker  规则重排：按 doc 去重（同文档只留最高分块）+ 问题关键词重叠加权
   —— 零依赖、确定性、可复现；生产意义：精排前先消除"同一文档刷屏 Top-K"的假命中
2. BGEReranker   bge-reranker-base（FlagEmbedding）：交叉编码精排，质量更高但需下载模型
   —— 装不动/无缓存时自动降级 RuleReranker，评测报告标注实际使用的版本

接口统一：rerank(query, candidates: list[dict], top_k=5) -> list[dict]（保序精排）
candidates 元素至少含 {chunk_id, doc_id, text, fused_score, source}。
"""
import re
from typing import Any

from app.shared import config

# 停用词（中文检索关键词重叠的噪音词）
_STOPWORDS = set(
    ["的", "了", "和", "与", "及", "是", "在", "我", "你", "他", "她", "它", "这", "那", "有", "就", "都", "而", "及", "或", "按", "根据", "需要", "应该", "什么", "怎么", "如何", "一个", "一笔", "公司", "规定", "吗", "呢", "请问", "是否", "具体", "到底", "分别", "哪个", "哪些", "多少", "多久", "之内", "以内", "以后", "时候", "处理", "流程", "管理", "规范", "办法", "文件", "部门"]
)


def _tokenize_cn(text: str) -> list[str]:
    """中文分词：jieba（与 BM25 同一分词器），去停用词，保留数字/条款号/英文。"""
    import jieba
    # 条款号/数字/英文单位先整块保留，再 jieba 切中文
    special = re.findall(r"第\s*[\d\-]+\s*条(?:第\s*\d+\s*款)?|[0-9]+(?:\.[0-9]+)?%?|[A-Za-z]+", text)
    words = [t.strip() for t in jieba.lcut(text) if t.strip()]
    return [t for t in (special + words) if t not in _STOPWORDS and len(t) > 0]


class RuleReranker:
    """规则重排（降级方案）：doc 去重 + RRF 保序，零加权。

    设计教训（Day2 实测，写入报告）：任何关键词重叠加权（即使小权重 0.02）都会干扰
    RRF 的融合排序，实测把 Hit@1 从 0.8974 拉低到 0.8782。原因：供应链制度文档的主题词
    （采购/审批/管理）几乎每篇都有，加权无区分度，反而破坏"数字/条款/跨文档"的融合信号。
    结论：**规则重排只做 doc 去重**（消除同文档刷屏、让 Top-K 覆盖更多文档），
    真正的语义精排交给 bge-reranker。生产意义：规则版是"无模型可用"的离线兜底，诚实标注。
    """

    name = "rule"

    def rerank(self, query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
        # RRF 顺序即最佳顺序（融合已是最强信号），仅做 doc 去重
        ranked, seen_docs = [], set()
        for c in candidates:
            if c["doc_id"] not in seen_docs:
                seen_docs.add(c["doc_id"])
                ranked.append(c)
            if len(ranked) >= top_k:
                break
        return ranked[:top_k]


class BGEReranker:
    """bge-reranker-base 交叉编码重排。

    不依赖 FlagEmbedding.compute_score（1.4.0 与新版 transformers 的 prepare_for_model
    API 不兼容），直接用 transformers 的 AutoModelForSequenceClassification 前向取 logit——
    更底层、更可控，也便于面试讲"交叉编码 vs 双塔"。

    模型加载失败（网络/显存/下载）时自动降级 RuleReranker，报告诚实标注。
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base"):
        self.model_name = model_name
        # transformers 模型/分词器/设备：Any 避免 deep 第三方类型（手册坑：不被第三方卡住）
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str | None = None
        self._load_error: str | None = None

    def _load(self):
        if self._model is None and self._load_error is None:
            try:
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer
                print(f"[reranker] 加载 bge-reranker {self.model_name}（首次下载 ~1GB）……")
                self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self._model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
                self._device = "cuda" if torch.cuda.is_available() else "cpu"
                if self._device == "cuda":
                    self._model = self._model.to(self._device)
                self._model.eval()
                print(f"[reranker] bge-reranker 就绪（device={self._device}）")
            except Exception as e:  # 网络/显存/下载失败 → 降级
                self._load_error = str(e)
                print(f"[reranker] bge-reranker 加载失败，降级规则重排：{e}")
        return self._model

    @property
    def name(self) -> str:
        if self._model is not None:
            return f"bge({self.model_name.rsplit('/', 1)[-1]})"
        return "bge-failed→rule" if self._load_error else "bge(pending)"

    def _score(self, query: str, texts: list[str]) -> list[float]:
        import torch
        import torch.nn.functional as F
        pairs = [[query, t] for t in texts]
        inputs = self._tokenizer(pairs, padding=True, truncation=True, max_length=512,
                                 return_tensors="pt")
        if self._device == "cuda":
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
        with torch.no_grad():
            logits = self._model(**inputs).logits
        # bge-reranker-base 双类输出，取正例 logit 的 sigmoid 作为相关性分
        scores = logits.squeeze(-1) if logits.shape[-1] == 1 else logits[:, 1]  # 索引 1 为"相关"
        return [float(s) for s in F.sigmoid(scores)]

    def rerank(self, query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
        model = self._load()
        if model is None:
            return RuleReranker().rerank(query, candidates, top_k=top_k)  # 自动降级
        texts = [c["text"] for c in candidates]
        try:
            scores = self._score(query, texts)
        except Exception as e:
            self._load_error = str(e)
            print(f"[reranker] bge 打分失败，降级规则重排：{e}")
            return RuleReranker().rerank(query, candidates, top_k=top_k)

        # 相关性分降序 + doc 去重后取 top_k
        indexed = sorted(zip(candidates, scores, strict=False), key=lambda x: -float(x[1]))
        ranked, seen_docs = [], set()
        for c, _sc in indexed:
            if c["doc_id"] not in seen_docs:
                seen_docs.add(c["doc_id"])
                ranked.append(c)
            if len(ranked) >= top_k:
                break
        return ranked[:top_k]


def get_reranker(force: str | None = None):
    """重排器工厂：LLM_RERANKER=rule|bge，默认 bge（失败自动降级 rule）。"""
    import os
    choice = (force or os.getenv("LLM_RERANKER") or "bge").lower()
    if choice == "bge":
        return BGEReranker()
    return RuleReranker()
