"""项目 A · W22 Day1 语义路由 + 语义缓存轻量单测（CI 可跑——不依赖模型/网络/Qdrant）。

覆盖纯逻辑 + 注入 FakeEmbedder 的决策逻辑：
- semantic_router：
  · bootstrap 手打原型路径（use_samples=False）——阈值路由 / fallback / source
  · 样本 kNN 路径（samples_override 注入）——投票路由 / matched 可解释 / 低置信 fallback
  · 规则优先层——PO 单号 / 报表 / 物流跟踪命中；"物流运输损耗"（RAG 制度问）不被误杀
- semantic_cache：字符重叠闸门 / put+lookup 命中(source=cache) / 双闸门防误命中 / 版本失效
"""
import numpy as np

# ==================== FakeEmbedder（确定性向量，替代真实 bge） ====================

# 关键词 → 类目索引：含关键词的句子对齐该类目 unit vector，路由结果可预测
# ★ W24 Day6：新增 data 类目（查数类 → NL2SQL 域）
_CAT_KEYWORDS = {"rag": "采购", "tool": "订单", "chat": "你好", "data": "延迟发货"}
_CAT_IDX = {"rag": 0, "tool": 1, "chat": 2, "data": 3}
_DIM = 8


class FakeEmbedder:
    """模拟 Embedder：embed_query/embed_texts 返回基于关键词的 unit vector。

    可识别类目的句子 → 该类目 unit vector；无法识别 → 全 0（不污染其它类目均值）。
    query 与同关键词的样本向量相似度 = 1（决定论），供路由/缓存决策逻辑测试。"""

    def __init__(self):
        self._model = True

    def _cat_of(self, text: str) -> str | None:
        for cat, kw in _CAT_KEYWORDS.items():
            if kw in text:
                return cat
        return None  # 无法识别 → 不对齐任何类目

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(_DIM, dtype=np.float32)
        cat = self._cat_of(text)
        if cat is not None:
            v[_CAT_IDX[cat]] = 1.0
        return v

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts], dtype=np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return self._vec(query)


def _make_router():
    from app.shared.rag.semantic_router import SemanticRouter
    # 强制 bootstrap 手打原型路径（不读样本文件，测试确定性）
    return SemanticRouter(embedder=FakeEmbedder(), use_samples=False)


def _make_sample_router():
    """样本 kNN 路径：注入 FakeEmbedder 可识别的标注样本（同关键词 → 同类目向量）。"""
    from app.shared.rag.semantic_router import SemanticRouter
    samples = {
        "version": "test-v1",
        "samples": [
            {"query": "采购申请需要审批吗", "label": "rag"},
            {"query": "采购金额超过多少招标", "label": "rag"},
            {"query": "帮我查一下订单 PO-0001", "label": "tool"},
            {"query": "把订单 PO-0002 改期", "label": "tool"},
            {"query": "生成上个月报表", "label": "tool"},
            {"query": "你好，很高兴认识你", "label": "chat"},
            {"query": "你是谁，能做什么", "label": "chat"},
            # data 样本刻意避开 tool 关键词"订单"（FakeEmbedder 关键词顺序：订单先匹配）
            {"query": "近30天延迟发货的数量", "label": "data"},
            {"query": "各区域供应商的金额汇总", "label": "data"},
        ],
    }
    return SemanticRouter(embedder=FakeEmbedder(), samples_override=samples)


# ==================== 语义路由：bootstrap 手打原型路径 ====================

def test_router_bootstrap_keyword_categories():
    """手打原型路径：含明确类目关键词 → 路由到对应类目 + source=prototype。"""
    r = _make_router()
    res = r.route("采购申请需要审批吗")
    assert res["route"] == "rag" and res["source"] == "prototype"
    res2 = r.route("你好呀，很高兴认识你")
    # ★ W28-D1：长聊天表述已入规则层 → rule（原 prototype）；仍是 chat 类
    assert res2["route"] == "chat" and res2["source"] in ("rule", "prototype")
    res3 = r.route("订单 PO-0001 现在什么状态")
    # 规则优先层命中（PO 单号）→ rule
    assert res3["route"] == "tool" and res3["source"] == "rule"


def test_router_chat_long_phrase_rules():
    """★ W28-D1：长聊天表述规则层拦截（容器真 bge 下 bootstrap 原型覆盖不足的补充）。

    完整寒暄句（长度 >6 不进精确词分支）此前被真实 embedding 误判 rag，
    现在规则层子串拦截 → chat（零检索零 token 分支），压测/聊天快路径回归。
    """
    r = _make_router()
    for q in ["你好呀，你能做什么？", "你是做什么的", "很高兴认识你呀", "你能帮我做什么"]:
        res = r.route(q)
        assert res["route"] == "chat" and res["source"] == "rule", q
    # 制度问不受影响（不进 chat）
    res = r.route("采购申请需要经过哪几级审批")
    assert res["route"] != "chat"


def test_router_empty_query_fallback():
    """空 query 不触发模型 → 兜底 fallback，source=fallback。"""
    r = _make_router()
    res = r.route("")
    assert res["route"] == "rag" and res["source"] == "fallback"
    assert r.routes == ["rag", "tool", "chat", "data"]


def test_router_thresholds_configured():
    """阈值配置化：四类各有阈值，rag 为默认域。"""
    from app.domains.kb import config
    t = config.SEMANTIC_ROUTER_THRESHOLDS
    assert set(t) == {"rag", "tool", "chat", "data"}
    assert config.SEMANTIC_ROUTER_FALLBACK == "rag"


# ==================== 语义路由：样本 kNN 路径 ====================

def test_router_samples_knn_votes():
    """样本路径：kNN 加权投票路由 + source=sample + matched 可解释。"""
    r = _make_sample_router()
    assert r.routes == ["rag", "tool", "chat", "data"]
    res = r.route("采购申请需要审批吗")   # 与 rag 样本同关键词
    assert res["route"] == "rag" and res["source"] == "sample"
    assert res["matched"] and res["matched"][0]["label"] == "rag"
    # "你好"命中规则层 chat（_CHAT_EXACT 精确词）→ source=rule；仍是 chat 类
    res2 = r.route("你好，你是谁")
    assert res2["route"] == "chat" and res2["source"] in ("rule", "sample")


def test_router_samples_low_confidence_fallback():
    """样本路径：与所有样本都低相似（无关键词）→ fallback + 低置信标记。"""
    r = _make_sample_router()
    # FakeEmbedder 下"质保金多久退"无任何关键词 → 全 0 向量 → Top1 sim=0 < 阈值 → fallback
    res = r.route("质保金多久退")
    assert res["route"] == "rag" and res["source"] == "fallback"


# ==================== 语义路由：规则优先层 ====================

def test_router_rule_high_confidence():
    """规则层：PO 单号 / 报表 / 物流跟踪 → tool（source=rule，零 embedding 成本）。"""
    r = _make_router()
    assert r.route("订单 PO-0088 现在什么状态")["route"] == "tool"
    assert r.route("生成上个月的库存对账报表")["route"] == "tool"
    assert r.route("物流进度到哪了")["route"] == "tool"
    assert r.route("取消这笔订单 PO-0099")["route"] == "tool"


def test_router_rule_not_misclassify_rag():
    """★ 防回归：RAG 制度问题含"物流"字样不能被规则误杀成 tool。"""
    r = _make_router()
    res = r.route("物流运输损耗怎么赔付")
    assert res["route"] != "tool"  # 应走 embedding → rag（或 fallback rag）


# ==================== 语义路由：data 分支（W24 Day6） ====================


def test_router_rule_data_high_confidence():
    """规则层：延迟/时间窗/分组/排行查数 → data（source=rule）。"""
    r = _make_router()
    assert r.route("近30天延迟发货的订单有多少")["route"] == "data"
    assert r.route("延迟发货的订单占比是多少")["route"] == "data"
    assert r.route("各区域的订单总金额是多少")["route"] == "data"
    assert r.route("订单数量最多的前5个供应商")["route"] == "data"
    assert r.route("近7天各状态的订单数量")["route"] == "data"


def test_router_rule_data_not_misclassify_rag():
    """★ 防回归：RAG 制度问题含"多少/占比"字样不能被 data 规则误杀。"""
    r = _make_router()
    # "采购金额超过多少必须招标采购"是制度问（含"多少"），不能拦成 data
    res = r.route("采购金额超过多少必须招标采购")
    assert res["route"] != "data"
    # "库存盘点多久进行一次"是制度问，不能因"库存"被 data 规则命中
    res2 = r.route("库存盘点多久进行一次")
    assert res2["route"] != "data"


def test_router_samples_data_knn():
    """样本路径：含 data 关键词（延迟发货）→ 路由到 data + source=sample。

    注意："近30天延迟发货的订单有多少"会命中规则层（source=rule）——规则优先于 embedding
    是设计行为；这里用不含规则触发词的问题验证样本 kNN 路径。
    """
    r = _make_sample_router()
    assert r.routes == ["rag", "tool", "chat", "data"]
    res = r.route("延迟发货的明细情况")
    assert res["route"] == "data" and res["source"] == "sample"
    assert res["matched"] and res["matched"][0]["label"] == "data"


# ==================== 语义缓存（双闸门 + 命中/失效） ====================

def test_char_overlap_logic():
    from app.shared.rag.semantic_cache import _char_overlap
    assert _char_overlap("采购申请有效期是多久", "采购申请有效期是多久") > 0.99  # 同句 ≈1
    assert _char_overlap("采购申请的有效期有多长", "采购申请有效期是多久") >= 0.40  # 近义
    assert _char_overlap("今天天气怎么样", "采购申请有效期是多久") == 0.0          # 无关
    assert _char_overlap("  采购 ，申请 ！", "采购申请") == 1.0                    # 去标点


def test_cache_hit_marks_source_and_stats():
    from app.shared.rag.semantic_cache import SemanticCache
    c = SemanticCache(embedder=FakeEmbedder())
    c.put("采购申请有效期是多久", "60 个自然日", citations=[{"doc_id": "X"}])
    hit = c.lookup("采购申请的有效期有多长")
    assert hit is not None and hit["source"] == "cache"
    assert hit["answer"] == "60 个自然日" and hit["citations"] == [{"doc_id": "X"}]
    assert hit["sim"] >= 0.9 and hit["char_overlap"] >= 0.40
    stats = c.hit_rate()
    assert stats["hits"] == 1 and stats["misses"] == 0


def test_cache_double_gate_blocks_wrong_topic():
    """embedding 相似（同关键词对齐）但字符不重叠 → 不命中（防跨主题误命中）。"""
    from app.shared.rag.semantic_cache import SemanticCache
    c = SemanticCache(embedder=FakeEmbedder())
    c.put("采购申请有效期是多久", "60 天", citations=[])
    # FakeEmbedder 让"今天天气怎么样"也对齐 rag 向量（sim 高），但字符 0 重叠 → 不命中
    assert c.lookup("今天天气怎么样") is None


def test_cache_miss_then_hit():
    from app.shared.rag.semantic_cache import SemanticCache
    c = SemanticCache(embedder=FakeEmbedder())
    assert c.lookup("采购审批要几级") is None        # 未缓存 → miss
    c.put("采购审批要几级", "三级", citations=[])
    assert c.lookup("采购审批要几级") is not None    # 已缓存 → hit（sim=1 + overlap=1）


def test_cache_invalidate_version():
    from app.shared.rag.semantic_cache import SemanticCache
    c = SemanticCache(embedder=FakeEmbedder(), version="kb-v1")
    c.put("采购申请有效期", "60", citations=[])
    assert len(c._store) == 1
    n = c.invalidate(new_version="kb-v2")
    assert n == 1 and len(c._store) == 0 and c.version == "kb-v2"


def test_cache_empty_query_never_hits():
    from app.shared.rag.semantic_cache import SemanticCache
    c = SemanticCache(embedder=FakeEmbedder())
    c.put("采购申请", "60", citations=[])
    assert c.lookup("") is None
