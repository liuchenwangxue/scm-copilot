"""语义路由（W22 Day1 + 生产化重构）：让每个请求"只走该走的链路"。

为什么（面试 44 题/高并发 & 46 题/检索优化素材）：
- 一个 Agent 往往面对多种请求：制度知识问答（RAG）、业务查单改单（工具链）、闲聊寒暄（chat）。
  全部塞进 RAG 主链路 → 闲聊白白烧检索+LLM，业务操作被当成制度问题答错。
- 语义路由用"query embedding 与标注样本比相似度"做意图分流，一次 embedding 开销（几毫秒）
  换来：不相关请求不进 RAG、语义相似问题走缓存不再重算。

★ 生产化（不靠手打原型句）：
- 类目判定 = **kNN 加权投票**：query embedding 与所有标注样本比相似度，Top-K 近邻按
  类目投票（相似度加权）。比"类目向量中心"更稳——rag 类样本跨 8 主题、中心会被
  平均成"四不像"，而近邻投票对分布广的类目鲁棒（实测两种方案 30 条测试集都 100%，
  但 kNN 近邻可直接解释/回流）。
- 标注样本 = `data/semantic_router_samples.json`：真实 query（评测集抽取/日志）+ 标注。
  新增意图 → 追加样本 + bump version → 跑 `scripts/build_router_prototypes.py` 重建向量缓存。
- config 里的手打原型句仅作 **bootstrap 兜底**（无样本文件时用，避免冷启动裸奔）。
- 规则优先层：高置信模式（tool 的 PO 订单号等）先拦，不依赖 embedding。
- 向量缓存文件：样本向量持久化（`data/semantic_router_vectors.json`），启动免重算。
- 可解释：route() 返回命中的代表样本（`matched`），低置信可回流标注（数据闭环）。

设计要点（对应手册坑）：
- 阈值分开调：RAG 是默认域（宽容阈值，宁可进 RAG 也别漏）；tool/chat 从严（防误路由到错误链路）。
  理由：误把 RAG 请求路由到闲聊 = 答非所问（糟）；误把闲聊路由进 RAG = 白烧 token（可接受）。
  （样本路径阈值基于"最近邻居相似度"：rag 0.60 / tool 0.80 / chat 0.85，见 config。）
- 无类目达标 → 兜底到默认域 rag（SEMANTIC_ROUTER_FALLBACK），保证服务不裸奔。
- 路由结果打日志（event=semantic_route, source={sample|prototype|rule|fallback}），供审计/回流。

接口：
    SemanticRouter(embedder=None)     # embedder 复用 Embedder（bge-small-zh）
    .route(query) -> dict             # {"route", "score", "scores", "source", "matched"}
    .proto_vectors()                  # 类目原型向量（构建/调试用）
"""
import json
from pathlib import Path

import numpy as np

from app.shared import config
from app.shared.rag.embedder import Embedder


def cos_sim(a: np.ndarray, b: np.ndarray) -> float:
    """余弦相似度（向量均已归一化时即点积）。"""
    return float(np.dot(a, b))


# kNN 投票近邻数（样本路径；K 太小噪声大，太大稀释——5 实测 30 条 100%）
_KNN_K = 5


# 规则优先层：高置信模式直接定路由（不依赖 embedding，零成本、可审计）
# (正则, route, 说明) 列表，命中即短路。
# ★ 规则要"精确到高置信模式"，别用裸关键词——"物流运输损耗怎么赔付"是 RAG 制度问题，
#   不能因含"物流"就拦成 tool（误杀比不拦更糟，落到 RAG 只是白烧 token）。
_RULES = [
    # 业务单号模式：PO-XXXX / 订单 PO → tool（订单业务操作）
    (r"po[-_ ]?\d{2,}", "tool", "含业务单号 PO- 模式"),
    (r"订单\s*[a-zA-Z]*\d", "tool", "含订单+编号"),
    (r"报表|对账|汇总|导出|生成.*报表", "tool", "报表类业务"),
    (r"查.*单|订单.*状态|在途|物流(进度|到哪|跟踪|状态|在哪)", "tool", "查单/物流跟踪"),
    (r"把.*改|修改.*单|取消.*单|作废.*单|撤.*单", "tool", "改/取消/作废订单"),
    # ★ W24 Day6 data 分支规则：高置信"组合模式"（手册坑：别用裸关键词，防误杀 RAG 制度问——
    #   "采购金额超过多少必须招标采购"含"多少"但它是制度问题，不能拦成 data）。
    (r"(延迟发货|发货).*(多少|占比|统计|分布|最高|最多)", "data", "延迟/发货指标查数"),
    (r"(近\d+天|上个月).*(订单|发货|金额|销量|库存|供应商|商品).*(多少|统计|分布)", "data", "时间窗经营指标查数"),
    (r"(各区域|各仓库|各状态|各供应商|各承运商|各类目).*(订单|金额|数量|库存|销量|占比|分布|统计)", "data", "分组维度经营指标查数"),
    (r"(TOP\d*|前\d+).*(供应商|商品|订单|区域|仓库|承运商)", "data", "排行类查数"),
]
# 闲聊高频精确词（整句命中率高才用规则，避免误杀）
_CHAT_EXACT = {
    "你好", "您好", "再见", "谢谢", "拜拜", "在吗", "周末愉快", "早上好", "晚上好",
    "你好呀", "您好呀", "辛苦了", "不客气", "收到",
}
# ★ W28-D1（容器口径统一 C1）：长聊天表述子串匹配——容器装真 bge 后语义路由
#   "假死"变"真活"，暴露 bootstrap 聊天原型覆盖不足："你好呀，你能做什么？"这类
#   完整表述与 chat 原型相似度仅 ~0.52（< 阈值 0.85）被误判 rag → 触发 RAG 检索
#   长尾。规则优先层补拦截（零 embedding 成本，chat 是零检索零 token 分支）。
_CHAT_PHRASES = [
    "你能做什么", "你是做什么的", "你是谁", "很高兴认识你", "认识你很高兴",
    "你好呀", "您好呀", "你能帮我做什么", "是做什么的",
]


def _load_samples() -> dict | None:
    """加载标注样本文件（生产主路径）。无文件 → None（走 bootstrap 手打原型）。"""
    p = config.SEMANTIC_ROUTER_SAMPLES_FILE
    try:
        if Path(p).exists():
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            return {"version": data.get("version", "v1"),
                    "samples": data.get("samples", [])}
    except Exception as e:
        print(f"[semantic_router] 样本文件加载失败，退回 bootstrap: {type(e).__name__}: {str(e)[:60]}")
    return None


class SemanticRouter:
    """语义路由器：embedding 与类目原型比相似度 → 阈值路由。

    类目原型优先级：
      1. 标注样本中心（样本文件存在时，生产路径）→ source=sample
      2. config 手打原型（无样本文件时的 bootstrap）→ source=prototype
    """

    def __init__(self, embedder: Embedder | None = None, use_samples: bool | None = None,
                 samples_override: dict | None = None):
        """use_samples: None=自动检测样本文件；True=强制样本路径；False=强制 bootstrap 手打。
        samples_override: 测试注入自定义样本（{"version","samples":[{"query","label"}]}）。"""
        self.embedder = embedder or Embedder()
        self.thresholds = config.SEMANTIC_ROUTER_THRESHOLDS
        self.fallback = config.SEMANTIC_ROUTER_FALLBACK
        self._proto_vectors: dict[str, np.ndarray] | None = None
        self._sample_vecs: np.ndarray | None = None      # (N, dim) 标注样本向量
        self._sample_meta: list[dict] | None = None      # [{"query","label"}]
        self._sample_version: str | None = None
        self._samples_data: dict | None = None           # 标注样本（生产主路径）
        self.prototypes: dict[str, list[str]] | None = None  # bootstrap 手打原型（bootstrap 用）
        # 生产主路径：标注样本 → 类目中心
        self._samples_from_override = samples_override is not None
        if samples_override is not None:
            self._samples_data = samples_override
        elif use_samples is not False:
            self._samples_data = _load_samples()
        if self._samples_data:
            self._build_from_samples()
        else:
            self.prototypes = config.SEMANTIC_ROUTER_PROTOTYPES  # bootstrap 兜底

    # ---------------- 标注样本 → 类目原型 ----------------

    def _build_from_samples(self) -> None:
        """从标注样本构建：样本向量缓存（文件命中则免重算）+ 类目中心。"""
        data = self._samples_data
        assert data is not None
        self._sample_meta = data["samples"]
        self._sample_version = data["version"]
        if not self._samples_from_override:
            cache = self._load_vector_cache()
            if cache is not None and cache.get("version") == self._sample_version:
                self._sample_vecs = np.array(cache["sample_vecs"], dtype=np.float32)
                self._proto_vectors = {k: np.array(v, dtype=np.float32)
                                       for k, v in cache["proto_vectors"].items()}
                return
        # 未命中缓存 / 测试注入 → 现场计算（测试注入不读写缓存文件）
        # ★ 样本向量必须用 embed_query（query 口径）：标注样本=用户会问的话，与 route 时
        #   的 embed_query 同口径（bge 的 query/passage 指令不同，混用会自匹配掉到 ~0.82）
        queries = [s["query"] for s in self._sample_meta]
        self._sample_vecs = np.array([self.embedder.embed_query(q) for q in queries],
                                     dtype=np.float32)  # (N, dim) 已归一化
        proto: dict[str, np.ndarray] = {}
        labels = [s["label"] for s in self._sample_meta]
        for label in dict.fromkeys(labels):
            idx = [i for i, lb in enumerate(labels) if lb == label]
            mean = self._sample_vecs[idx].mean(axis=0)
            norm = float(np.linalg.norm(mean))
            proto[label] = mean / norm if norm > 1e-12 else mean
        self._proto_vectors = proto
        if not self._samples_from_override:
            self._save_vector_cache()

    def _vector_cache_path(self) -> Path:
        return Path(config.SEMANTIC_ROUTER_VECTORS_FILE)

    def _load_vector_cache(self) -> dict | None:
        try:
            p = self._vector_cache_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def _save_vector_cache(self) -> None:
        try:
            assert self._proto_vectors is not None and self._sample_vecs is not None
            payload = {
                "version": self._sample_version,
                "proto_vectors": {k: v.tolist() for k, v in self._proto_vectors.items()},
                "sample_vecs": self._sample_vecs.tolist(),
            }
            self._vector_cache_path().write_text(
                json.dumps(payload), encoding="utf-8")
        except Exception as e:
            print(f"[semantic_router] 向量缓存写回失败（不阻塞）: {str(e)[:60]}")

    # ---------------- 路由 ----------------

    @property
    def routes(self) -> list[str]:
        if self._samples_data:
            assert self._sample_meta is not None
            labels = [s["label"] for s in self._sample_meta]
            return list(dict.fromkeys(labels))
        return list(self.prototypes or {})

    def proto_vectors(self) -> dict[str, np.ndarray]:
        """类目原型向量（中心），与嵌入口径一致（L2 归一化）。"""
        if self._proto_vectors is not None:
            return self._proto_vectors
        # bootstrap 路径：config 手打原型句 → 均值
        vecs: dict[str, np.ndarray] = {}
        for route, queries in (self.prototypes or {}).items():
            if not queries:
                continue
            emb = self.embedder.embed_texts(queries)
            mean = emb.mean(axis=0)
            norm = float(np.linalg.norm(mean))
            vecs[route] = mean / norm if norm > 1e-12 else mean
        self._proto_vectors = vecs
        return vecs

    def _rule_route(self, query: str) -> dict | None:
        """规则优先层：高置信模式短路（零 embedding 成本）。命中返回路由 dict。"""
        import re
        q = query.strip()
        # 闲聊精确词（整句极短且命中常见寒暄）
        for w in _CHAT_EXACT:
            if q == w or (len(q) <= 6 and w in q):
                return {"route": "chat", "score": 1.0, "scores": {},
                        "source": "rule", "matched": [{"query": q, "label": "chat", "why": "闲聊高频词"}]}
        # ★ W28-D1：长聊天表述子串拦截（完整寒暄表述，非裸关键词，误杀风险低）
        for p in _CHAT_PHRASES:
            if p in q:
                return {"route": "chat", "score": 1.0, "scores": {},
                        "source": "rule", "matched": [{"query": q, "label": "chat", "why": "长聊天表述"}]}
        for pat, route, why in _RULES:
            if re.search(pat, q, re.IGNORECASE):
                return {"route": route, "score": 1.0, "scores": {},
                        "source": "rule", "matched": [{"query": q, "label": route, "why": why}]}
        return None

    def route(self, query: str) -> dict:
        """路由：返回 {"route", "score", "scores", "source", "matched"}。

        source: rule（规则命中）| sample（标注样本 kNN）| prototype（bootstrap 手打）| fallback。
        score: 胜出类的 Top1 邻居相似度（与手册"相似度≥阈值"口径一致）。
        """
        if not query.strip():
            return {"route": self.fallback, "score": 0.0, "scores": {},
                    "source": "fallback", "matched": []}
        # ① 规则优先层（零成本）
        rule_hit = self._rule_route(query)
        if rule_hit is not None:
            return rule_hit
        # ② embedding 路由
        qv = self.embedder.embed_query(query)
        if self._samples_data:
            return self._route_by_samples(query, qv)
        return self._route_by_prototype(qv)

    def _route_by_samples(self, query: str, qv: np.ndarray) -> dict:
        """样本路径（生产）：kNN 加权投票，score=Top1 邻居相似度。"""
        assert self._sample_vecs is not None and self._sample_meta is not None
        sims = self._sample_vecs @ qv  # (N,)
        top = np.argsort(-sims)[: _KNN_K]
        votes: dict[str, float] = {}
        for i in top:
            lb = self._sample_meta[i]["label"]
            votes[lb] = votes.get(lb, 0) + float(sims[i])
        best_route, _ = max(votes.items(), key=lambda kv: kv[1])
        top1_sim = round(float(sims[top[0]]), 4)
        scores = {rt: round(float(votes[rt]), 4) for rt in votes}
        matched = [{"query": self._sample_meta[i]["query"][:30],
                    "label": self._sample_meta[i]["label"],
                    "sim": round(float(sims[i]), 4)} for i in top[:3]]
        threshold = self.thresholds.get(best_route, 0.0)
        if top1_sim >= threshold:
            return {"route": best_route, "score": top1_sim, "scores": scores,
                    "source": "sample", "matched": matched}
        return {"route": self.fallback, "score": top1_sim, "scores": scores,
                "source": "fallback", "matched": matched}

    def _route_by_prototype(self, qv: np.ndarray) -> dict:
        """bootstrap 路径（无样本文件）：类目中心相似度 + 阈值。"""
        scores: dict[str, float] = {}
        for route, pv in self.proto_vectors().items():
            scores[route] = round(cos_sim(qv, pv), 4)
        best_route, best_score = max(scores.items(), key=lambda kv: kv[1])
        threshold = self.thresholds.get(best_route, 0.0)
        if best_score >= threshold:
            return {"route": best_route, "score": best_score, "scores": scores,
                    "source": "prototype", "matched": []}
        return {"route": self.fallback, "score": best_score, "scores": scores,
                "source": "fallback", "matched": []}


def log_route(query: str, result: dict, sink=None) -> None:
    """路由结果落日志（可注入 audit sink；无则打印，服务进程可 grep event=semantic_route）。"""
    entry = {
        "event": "semantic_route",
        "query": query[:80],
        "route": result["route"],
        "score": result["score"],
        "scores": result["scores"],
        "source": result["source"],
        "matched": result.get("matched", [])[:2],
    }
    if sink is not None:
        try:
            sink("semantic_route", detail=f"route={result['route']} score={result['score']} src={result['source']}")
            return
        except Exception:
            pass
    print(f"[semantic_router] {entry}")


if __name__ == "__main__":
    # 自检：打印类目原型来源 + 若干样例路由
    r = SemanticRouter()
    src = "sample" if r._samples_data else "prototype"
    print(f"[semantic_router] 类目：{r.routes}（原型来源={src}，"
          f"样本版本={r._sample_version or '-'}）")
    for route, v in r.proto_vectors().items():
        print(f"  原型[{route}] 维度={v.shape[0]}")
    samples = [
        "采购申请审批要经过哪些环节？",
        "帮我查一下订单 PO-0005 现在什么状态",
        "你好呀，你是做什么的？",
        "供应商年度评估的打分维度",
        "质保金什么时候退",
    ]
    for s in samples:
        print(f"  {s!r:24} -> {r.route(s)}")
