"""★ Schema Linking（W24 Day4）——问题 → 相关表召回 Top-3 → 只注入相关 schema。

对应《W24学习执行手册》Day4 +《03》1.3 节：
- 语料：每表一条描述（表名 + 中文用途 + 关键列语义）+ 每列一条描述（含勾稽关系）；
  勾稽关系（手册坑）：`orders.amount = Σ(order_items.amount)`、`order_items.amount = quantity × unit_price`
  必须写进语料，否则 join 类问题模型瞎猜关联列；
- 复用 shared 的 bge-small embedding（`app.shared.rag.embedder.Embedder`）：
  问题向量（query 指令）→ 与语料项向量（passage）点积 = 余弦相似度；
  每表得分 = 该表全部语料项（表描述 + 该表列描述）的相似度最高分；
- 召回阈值（手册坑）：Top-3 之外的表一律不进 prompt，宁可 linking 评测集标注更多；
- 召回结果映射回完整 DDL 片段（TABLE_DDL）+ 关联关系（v2 prompt 用）；
- 单例懒加载：embedding 模型首次加载后缓存，向量矩阵一次性编码缓存。

接口：
    linker = SchemaLinker()
    linker.link_tables(question, top_k=3) -> list[str]     # Top-3 相关表名
    linker.build_schema_text(tables) -> str                # 召回表 DDL 片段
    linker.link_details(question, top_k=3) -> dict         # 调试用：每表得分明细

验收指标（手册）：召回准确率 ≥90%（该在的表在 Top-3 里）；prompt token 降 ≥50%。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.shared.rag.embedder import Embedder

# ==================== 每张表完整 DDL 描述（prompts.py SCHEMA_TEXT 的单一数据源） ====================
# 注意：改动此处会同时影响 v1 全 schema 注入与 v2 召回注入——保持两版口径一致。

TABLE_DDL: dict[str, str] = {
    "orders": """orders（订单主表）:
  id            BIGINT 主键
  order_no      CHAR(16) 唯一，格式 SO-YYYYMMDD-XXXX
  supplier_id   INT 供应商ID（关联 suppliers.id）
  region        ENUM 区域：华东/华北/华南/西南
  status        ENUM 状态：draft草稿/paid已支付/shipped已发货/done已完成/cancelled已取消
  amount        DECIMAL(12,2) 订单总金额（=该订单全部明细行金额之和，勾稽）
  remark        TEXT 备注（加急/客户指定承运商/分批到货等）
  created_at    DATETIME 创建时间（时间类问题的过滤列，一律用此列）""",
    "order_items": """order_items（订单明细）:
  id            BIGINT 主键
  order_no      CHAR(16) 订单号（关联 orders.order_no，同一订单多行明细）
  product_id    INT 商品ID（关联 products.id）
  quantity      INT 数量
  unit_price    DECIMAL(10,2) 单价
  amount        DECIMAL(12,2) 行金额 = quantity × unit_price""",
    "products": """products（商品）:
  id            INT 主键
  sku           CHAR(32) 唯一，格式 SKU-XXXXXXXX
  name          VARCHAR(128) 商品名
  category      VARCHAR(32) 类目：电子元件/机械配件/包装材料/办公用品/五金工具/化工原料/纺织辅料/仓储设备
  unit_price    DECIMAL(10,2) 单价（10–5000）
  status        INT 状态（1 正常）
  created_at    DATETIME 创建时间""",
    "suppliers": """suppliers（供应商）:
  id            INT 主键
  supplier_code CHAR(16) 唯一，格式 SUP-XXXX
  name          VARCHAR(64) 名称（含区域词，如"华东鑫达xx有限公司"）
  region        ENUM 区域：华东/华北/华南/西南
  rating        INT 评分 60–95
  contact       VARCHAR(32) 联系方式
  created_at    DATETIME 创建时间""",
    "inventory": """inventory（库存）:
  id            INT 主键
  product_id    INT 商品ID（关联 products.id，每商品一条）
  warehouse     VARCHAR(32) 仓库：华东仓/华北仓/华南仓/西南仓
  qty           INT 在库数量
  safety_qty    INT 安全库存（qty < safety_qty 即低库存）
  updated_at    DATETIME 更新时间""",
    "shipments": """shipments（发货）:
  id            BIGINT 主键
  order_no      CHAR(16) 唯一 订单号（关联 orders.order_no；仅 shipped/done 状态订单有发货记录）
  carrier       VARCHAR(32) 承运商：顺丰/圆通/中通/德邦/京东物流
  tracking_no   VARCHAR(32) 运单号
  shipped_at    DATETIME 发货时间
  delivered_at  DATETIME 签收时间（done 状态订单才有）
  delay_days    INT 延迟发货天数（>0 即延迟发货）
  remark        TEXT 备注（"延迟发货"标记）""",
}

# 表间关联关系（与 v1 SCHEMA_TEXT 尾部一致）
RELATIONSHIPS_TEXT = """## 关联关系
- orders --(order_no)--> order_items --(product_id)--> products
- orders --(supplier_id)--> suppliers
- orders --(order_no)--> shipments（发货表 order_no 唯一，一单一条）"""

# ==================== 召回语料（表描述 + 列描述） ====================

TABLE_DESCRIPTIONS: dict[str, str] = {
    "orders": "订单主表：每行一笔采购订单，含订单号、供应商、区域、状态、订单总金额、创建时间；"
              "是订单域关联枢纽：明细经 order_no 关联、发货经 order_no 关联、供应商经 supplier_id 关联；"
              "订单总金额 = 该订单全部明细行金额之和（勾稽）",
    "order_items": "订单明细表：每行一个商品明细，属于某订单（order_no），含商品、数量、单价、行金额；"
                   "行金额 = 数量 × 单价（勾稽）；订单总金额 = 明细行金额合计（勾稽）；"
                   "按商品类目统计销量/金额需 join products",
    "products": "商品主数据：SKU、名称、类目（电子元件/机械配件等）、单价、状态；"
                "是'类目/品类'维度的归属表——任何按类目分组统计（销量/库存/金额/占比）都要 join 本表用 category 列；"
                "订单明细与库存都通过 product_id 关联商品",
    "suppliers": "供应商主数据：名称（含区域词）、区域、评分；订单通过 supplier_id 关联供应商；"
                 "供应商名称可用于按供应商分组统计",
    "inventory": "库存表：每商品一条，所在仓库、在库数量、安全库存；在库数量低于安全库存即低库存；"
                 "通过 product_id 关联商品；按类目统计库存需 join products",
    "shipments": "发货表：每订单一条，承运商、运单号、发货时间、签收时间、延迟发货天数；"
                 "延迟发货天数 > 0 即延迟发货；通过 order_no 关联订单",
}

COLUMN_DESCRIPTIONS: dict[str, str] = {
    # ---- orders ----
    "orders.order_no": "订单号，格式 SO-YYYYMMDD-XXXX，唯一；订单明细与发货都按它关联",
    "orders.supplier_id": "供应商ID，关联 suppliers.id",
    "orders.region": "订单下单区域（华东/华北/华南/西南）——按区域统计订单量/订单金额时用它分组",
    "orders.status": "订单状态：draft草稿/paid已支付/shipped已发货/done已完成/cancelled已取消",
    "orders.amount": "订单总金额，等于该订单全部明细行金额之和（勾稽）",
    "orders.created_at": "订单创建时间，时间窗过滤列（近N天一律用它）",
    # ---- order_items ----
    "order_items.order_no": "订单号，关联 orders.order_no（同订单多行明细）",
    "order_items.product_id": "商品ID，关联 products.id",
    "order_items.quantity": "购买数量",
    "order_items.unit_price": "成交单价",
    "order_items.amount": "行金额 = 数量 × 单价（勾稽）；订单总金额 = 该订单各行金额合计（勾稽）",
    # ---- products ----
    "products.sku": "商品编码，格式 SKU-XXXXXXXX，唯一",
    "products.name": "商品名称",
    "products.category": "商品类目：电子元件/机械配件/包装材料/办公用品/五金工具/化工原料/纺织辅料/仓储设备；"
                        "凡'按类目/品类分组统计'（各类目销量/库存/金额/占比）都用它做分组列",
    "products.unit_price": "商品单价（10–5000）；计算库存总值（在库数量 × 单价）等金额指标需要它",
    "inventory.qty": "在库数量；与 products.unit_price 相乘得库存金额（库存总值）",
    # ---- suppliers ----
    "suppliers.supplier_code": "供应商编码，格式 SUP-XXXX，唯一",
    "suppliers.name": "供应商名称，含区域词（如'华东鑫达xx有限公司'），按供应商统计时用它做分组列",
    "suppliers.region": "供应商注册区域（华东/华北/华南/西南）——按区域统计供应商数量/评分时用它分组",
    "suppliers.rating": "供应商评分（60–95）",
    # ---- inventory ----
    "inventory.product_id": "商品ID，关联 products.id，每商品一条库存",
    "inventory.warehouse": "库存所在仓库（华东仓/华北仓/华南仓/西南仓）——按仓库分组统计库存量时用它",
    "inventory.safety_qty": "安全库存，在库数量低于它即低库存",
    # ---- shipments ----
    "shipments.order_no": "订单号，唯一，关联 orders.order_no（仅 shipped/done 订单有发货记录）",
    "shipments.carrier": "承运商：顺丰/圆通/中通/德邦/京东物流",
    "shipments.tracking_no": "运单号",
    "shipments.shipped_at": "发货时间",
    "shipments.delivered_at": "签收时间（done 状态订单才有）",
    "shipments.delay_days": "延迟发货天数，>0 即延迟发货（延迟发货统计用它）",
}


# ==================== 精简 DDL（v2 prompt 注入用，token 降本） ====================
# - 去掉列对齐空格（'  '.join 压缩为单空格）；
# - 省略低价值列：id 主键 / remark / contact / tracking_no / updated_at
#   （created_at/status/region/category 等业务关键列必须保留）。
# v1 全 schema 仍用完整 TABLE_DDL（Day3 基线口径不变）；v2 注入用本精简版。

_DROP_COLS = {"id", "remark", "contact", "tracking_no", "updated_at"}


def _compact_table_ddl(table: str) -> str:
    lines: list[str] = []
    for raw in TABLE_DDL[table].splitlines():
        if not raw.strip():
            continue
        col = raw.split()[0]
        if col != table and col in _DROP_COLS:
            continue
        lines.append("  ".join(raw.split()))
    return "\n".join(lines)


TABLE_DDL_COMPACT: dict[str, str] = {t: _compact_table_ddl(t) for t in TABLE_DDL}

# prompt 注入的召回裁剪阈值：Top-3 内保留分数 ≥ top1×0.75 的表（动态 1–3 张）。
# 依据《W24学习执行手册》Day4 坑："召回阈值别太松：Top-3 之外的表一律不进 prompt"——
# 裁剪只发生在 Top-3 内部，绝不注入 Top-3 之外的表。
PROMPT_MIN_RATIO = 0.75


def _corpus_entries() -> list[dict[str, str]]:
    """展开语料条目：每表一条（scope=table）+ 每列一条（scope=column）。"""
    entries: list[dict[str, str]] = []
    for table in TABLE_DDL:
        entries.append(
            {"scope": "table", "name": table, "text": TABLE_DESCRIPTIONS[table]}
        )
        for col_name, col_text in COLUMN_DESCRIPTIONS.items():
            if col_name.startswith(f"{table}."):
                entries.append(
                    {"scope": "column", "name": col_name, "text": col_text}
                )
    return entries


class SchemaLinker:
    """基于 bge-small 的表/列召回器（单例懒加载模型 + 语料向量缓存）。"""

    def __init__(self, top_k: int = 3) -> None:
        self.top_k = top_k
        self._embedder = Embedder()
        self._entries: list[dict[str, str]] | None = None
        self._vectors: np.ndarray | None = None  # (N, dim) 归一化语料向量

    # ---------- 语料与向量（惰性） ----------

    def _load_entries(self) -> list[dict[str, str]]:
        if self._entries is None:
            self._entries = _corpus_entries()
        return self._entries

    def _load_vectors(self) -> np.ndarray:
        if self._vectors is None:
            entries = self._load_entries()
            texts = [e["text"] for e in entries]
            self._vectors = self._embedder.embed_texts(texts)
        return self._vectors

    # ---------- 召回 ----------

    def link_tables(self, question: str, top_k: int | None = None) -> list[str]:
        """问题 → Top-3 相关表名（每表得分 = 该表全部语料项相似度最高分）。

        打分不依赖模型"听话"：确定性向量检索，Top-3 之外一律不进 prompt。
        """
        k = top_k or self.top_k
        scores = self._table_scores(question)
        return [t for t, _ in scores[:k]]

    def link_tables_scored(self, question: str, top_k: int | None = None) -> list[tuple[str, float]]:
        """Top-k 召回（带分数）——供 prompt 注入裁剪用。"""
        k = top_k or self.top_k
        return self._table_scores(question)[:k]

    def filter_prompt_tables(
        self, scored_tables: list[tuple[str, float]], min_ratio: float | None = None
    ) -> list[str]:
        """Top-3 内按相对 top1 分数裁剪 → 实际注入 prompt 的表（动态 1–3 张）。

        规则：保留分数 ≥ top1×min_ratio 的表；全部被裁时保底 top1。
        min_ratio 默认 PROMPT_MIN_RATIO=0.75（A/B 扫描 90 条包含率 100% + token 降 51%）。
        只发生在 Top-3 内部——绝不注入 Top-3 之外的表（手册坑）。
        """
        ratio = min_ratio if min_ratio is not None else PROMPT_MIN_RATIO
        if not scored_tables:
            return []
        top1 = scored_tables[0][1]
        out = [t for t, s in scored_tables if s >= top1 * ratio]
        return out or [scored_tables[0][0]]

    def link_prompt_tables(self, question: str) -> list[str]:
        """一步到位：召回 Top-3 → 裁剪 → 返回注入 prompt 的表列表（v2 用）。"""
        return self.filter_prompt_tables(self.link_tables_scored(question))

    def link_details(self, question: str, top_k: int | None = None) -> list[dict[str, Any]]:
        """调试/评测用：返回 [{table, score, hits: [语料项说明...]}] 明细。"""
        entries = self._load_entries()
        vectors = self._load_vectors()
        qv = self._embedder.embed_query(question)
        sims = np.asarray(qv) @ vectors.T  # (N,)
        per_table: dict[str, list[dict[str, Any]]] = {}
        for entry, sim in zip(entries, sims, strict=True):
            t = entry["name"].split(".")[0]
            per_table.setdefault(t, []).append(
                {"scope": entry["scope"], "name": entry["name"],
                 "text": entry["text"], "sim": float(sim)}
            )
        ranked = []
        k = top_k or self.top_k
        for t in sorted(per_table, key=lambda x: max(h["sim"] for h in per_table[x]), reverse=True)[:k]:
            hits = sorted(per_table[t], key=lambda h: h["sim"], reverse=True)[:3]
            ranked.append(
                {"table": t, "score": round(hits[0]["sim"], 4),
                 "hits": [{"name": h["name"], "sim": round(h["sim"], 4)} for h in hits]}
            )
        return ranked

    def _table_scores(self, question: str) -> list[tuple[str, float]]:
        entries = self._load_entries()
        vectors = self._load_vectors()
        qv = self._embedder.embed_query(question)
        sims = np.asarray(qv) @ vectors.T  # (N,)，向量已归一化 → 点积 = 余弦相似度
        best: dict[str, float] = {}
        for entry, sim in zip(entries, sims, strict=True):
            t = entry["name"].split(".")[0]
            best[t] = max(best.get(t, float("-inf")), float(sim))
        return sorted(best.items(), key=lambda kv: kv[1], reverse=True)

    # ---------- DDL 片段映射 ----------

    def build_schema_text(self, tables: list[str]) -> str:
        """召回表 → 精简 DDL 片段 + 关联关系（v2 prompt 只注入这些表）。

        ★ token 降本（Day4 A/B）：用 TABLE_DDL_COMPACT（去对齐空格 + 省略低价值列）；
        关联关系在注入表 ≥2 时才有意义，由调用方（prompts v2）按需追加。
        """
        parts = [TABLE_DDL_COMPACT[t] for t in tables if t in TABLE_DDL_COMPACT]
        if not parts:
            parts = [TABLE_DDL_COMPACT["orders"]]  # 防御性兜底：绝不注入空 schema
        return "## 数据库 schema（业务库 scm_biz，召回相关表）\n\n" + "\n\n".join(parts)


# 模块级单例（embedding 模型与向量只加载一次；跨进程无状态，多实例可各自缓存）
linker = SchemaLinker()
