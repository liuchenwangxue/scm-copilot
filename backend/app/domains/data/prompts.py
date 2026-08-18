"""NL2SQL prompt 模板 v1（W24 Day3）——全六表 schema 注入 + 5 条 few-shot。

对应《W24学习执行手册》Day3 上午 + 坑位：
- v1 用【全 schema 注入】（Day4 换 schema linking 召回相关表，此文件演进 v2）；
- few-shot 5 条：单表过滤 / 两表 join / 聚合分组 / 时间窗 / 排序各一；
- 日期口径：**时间窗示例由 `today` 驱动生成显式日期**（如 `created_at >= '2026-07-19'`），
  不用 `CURDATE() - INTERVAL 7 DAY`——评测集数据基准 BASE_DATE=2026-08-18（固定 seed），
  若用 CURDATE() 则运行日漂移导致空结果集被误判为 SQL 错（手册 Day1 坑）。
  评测传 today=BASE_DATE（数据基准日），生产传实际当天，few-shot 自动对齐；
- 规则强调：只读、必须 LIMIT、时间列用 created_at、今日日期显式给出。

接口：
    build_nl2sql_messages(question, today="2026-08-18") -> list[dict]
        system 指令 + user（schema + few-shot + 问题），符合 shared.llm messages 契约。
"""

from __future__ import annotations

from datetime import date, timedelta

# ---- 数据基准日：与 scripts/seed_biz.py BASE_DATE 对齐（评测时 today 传它）----
DATA_BASE_DATE = date(2026, 8, 18)

# ==================== 全六表 schema 描述（v1 全量注入） ====================
# 与 app/domains/data/models_biz.py 保持一致（含中文注释 + 勾稽关系，
# 帮助模型判断 join 关联列；Day4 schema_linker 语料也以此为底）。
SCHEMA_TEXT = """## 数据库 schema（业务库 scm_biz，六表）

orders（订单主表）:
  id            BIGINT 主键
  order_no      CHAR(16) 唯一，格式 SO-YYYYMMDD-XXXX
  supplier_id   INT 供应商ID（关联 suppliers.id）
  region        ENUM 区域：华东/华北/华南/西南
  status        ENUM 状态：draft草稿/paid已支付/shipped已发货/done已完成/cancelled已取消
  amount        DECIMAL(12,2) 订单总金额（=该订单全部明细行金额之和，勾稽）
  remark        TEXT 备注（加急/客户指定承运商/分批到货等）
  created_at    DATETIME 创建时间（时间类问题的过滤列，一律用此列）

order_items（订单明细）:
  id            BIGINT 主键
  order_no      CHAR(16) 订单号（关联 orders.order_no，同一订单多行明细）
  product_id    INT 商品ID（关联 products.id）
  quantity      INT 数量
  unit_price    DECIMAL(10,2) 单价
  amount        DECIMAL(12,2) 行金额 = quantity × unit_price

products（商品）:
  id            INT 主键
  sku           CHAR(32) 唯一，格式 SKU-XXXXXXXX
  name          VARCHAR(128) 商品名
  category      VARCHAR(32) 类目：电子元件/机械配件/包装材料/办公用品/五金工具/化工原料/纺织辅料/仓储设备
  unit_price    DECIMAL(10,2) 单价（10–5000）
  status        INT 状态（1 正常）
  created_at    DATETIME 创建时间

suppliers（供应商）:
  id            INT 主键
  supplier_code CHAR(16) 唯一，格式 SUP-XXXX
  name          VARCHAR(64) 名称（含区域词，如"华东鑫达xx有限公司"）
  region        ENUM 区域：华东/华北/华南/西南
  rating        INT 评分 60–95
  contact       VARCHAR(32) 联系方式
  created_at    DATETIME 创建时间

inventory（库存）:
  id            INT 主键
  product_id    INT 商品ID（关联 products.id，每商品一条）
  warehouse     VARCHAR(32) 仓库：华东仓/华北仓/华南仓/西南仓
  qty           INT 在库数量
  safety_qty    INT 安全库存（qty < safety_qty 即低库存）
  updated_at    DATETIME 更新时间

shipments（发货）:
  id            BIGINT 主键
  order_no      CHAR(16) 唯一 订单号（关联 orders.order_no；仅 shipped/done 状态订单有发货记录）
  carrier       VARCHAR(32) 承运商：顺丰/圆通/中通/德邦/京东物流
  tracking_no   VARCHAR(32) 运单号
  shipped_at    DATETIME 发货时间
  delivered_at  DATETIME 签收时间（done 状态订单才有）
  delay_days    INT 延迟发货天数（>0 即延迟发货）
  remark        TEXT 备注（"延迟发货"标记）

## 关联关系
- orders --(order_no)--> order_items --(product_id)--> products
- orders --(supplier_id)--> suppliers
- orders --(order_no)--> shipments（发货表 order_no 唯一，一单一条）"""

# ==================== few-shot 模板（5 条，时间窗由 today 驱动） ====================


def _date_ago(today: date, days: int) -> str:
    """today 往前 N 天的 ISO 日期字符串（few-shot/提示里的显式日期口径）。"""
    return (today - timedelta(days=days)).isoformat()


def build_few_shots(today: date | str) -> list[dict[str, str]]:
    """5 条 few-shot：单表过滤 / 两表 join / 聚合分组 / 时间窗 / 排序。

    today：数据基准日（评测传 BASE_DATE；生产传实际当天），时间窗示例自动对齐。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    recent30 = _date_ago(today, 30)

    return [
        # 1. 单表过滤
        {
            "question": "华东区域有多少订单？",
            "sql": "SELECT COUNT(*) AS cnt FROM orders WHERE region = '华东'",
        },
        # 2. 两表 join（订单明细 × 商品类目）
        {
            "question": "电子元件类目商品的累计销售金额是多少？",
            "sql": (
                "SELECT SUM(i.amount) AS total_amount FROM order_items i "
                "JOIN products p ON i.product_id = p.id WHERE p.category = '电子元件'"
            ),
        },
        # 3. 聚合分组
        {
            "question": "各区域的订单总金额是多少？",
            "sql": (
                "SELECT region, SUM(amount) AS total_amount FROM orders "
                "GROUP BY region ORDER BY total_amount DESC"
            ),
        },
        # 4. 时间窗（近 30 天，显式日期口径——避免模型各写各的）
        {
            "question": "近 30 天创建了多少订单？",
            "sql": f"SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '{recent30}'",
        },
        # 5. 排序 TOP N
        {
            "question": "金额最高的前 5 个订单有哪些？",
            "sql": "SELECT order_no, amount FROM orders ORDER BY amount DESC LIMIT 5",
        },
    ]


def build_nl2sql_messages(
    question: str,
    today: date | str = DATA_BASE_DATE,
) -> list[dict[str, str]]:
    """构建 NL2SQL 生成的 messages（system + user）。

    - system：角色 + 硬性规则（只读/必须 LIMIT/时间列/今日日期）；
    - user：schema（全六表）+ few-shot + 当前问题。
    返回格式符合 shared.llm.LLMProvider messages 契约。
    """
    if isinstance(today, str):
        today = date.fromisoformat(today)
    today_str = today.isoformat()

    system = (
        "你是 MySQL 8 专家。根据给定的数据库 schema 和用户问题，生成一条只读 SELECT 查询。\n"
        "硬性规则：\n"
        f"1. 只允许 SELECT（禁止 UPDATE/DELETE/INSERT/DDL）；今日日期为 {today_str}。\n"
        "2. 查询必须带 LIMIT 限制返回行数（默认 LIMIT 200；TOP N 类问题用 ORDER BY ... LIMIT N）。\n"
        "3. 时间类过滤条件一律用 created_at 列；'近 N 天' 写作 created_at >= '<今日-N天>' 的显式日期。\n"
        "4. 只能查询 schema 中出现的表和列；关联用给出的关联关系（order_no/product_id/supplier_id）。\n"
        "5. 只输出一条 SQL，不要输出解释、Markdown 代码块或分号以外的多余内容。"
    )

    few_shots = build_few_shots(today)
    few_shot_text = "\n".join(
        f"问题：{fs['question']}\nSQL：{fs['sql']}" for fs in few_shots
    )

    user = (
        f"{SCHEMA_TEXT}\n\n"
        f"## 示例（{len(few_shots)} 条）\n{few_shot_text}\n\n"
        f"## 问题\n{question}"
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
