"""NL2SQL 评测集 v1 生成器（W24 Day3）——50 条三层评测集，固定 gold SQL。

对应《W24学习执行手册》Day3 下午：
- 单表 30：过滤（区域/状态/时间窗）、排序 TOP N、去重计数
- join 20：订单+明细金额汇总、订单+供应商按区域聚合、商品+库存低库存
- 每条含 gold SQL（跑出的结果集即标准答案，固定 seed 数据保证稳定可复现）

输出：backend/evals/nl2sql_eval_v1.jsonl（每行一条 JSON）
字段：{id, layer: single|join, category, question, gold_sql}

时间窗口径：数据基准 BASE_DATE=2026-08-18（scripts/seed_biz.py），
"近7天/近30天" 用显式日期（>= '2026-08-11' / '2026-07-19'），避免运行日漂移。

用法：python -X utf8 backend/scripts/gen_eval_set_v1.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "evals" / "nl2sql_eval_v1.jsonl"

# ---------------- 单表 30（layer=single） ----------------

SINGLE_CASES: list[tuple[str, str, str]] = [
    # (question, category, gold_sql)
    ("华东区域有多少订单？", "filter-region",
     "SELECT COUNT(*) AS cnt FROM orders WHERE region='华东'"),
    ("华北区域已支付的订单有多少？", "filter-region-status",
     "SELECT COUNT(*) AS cnt FROM orders WHERE region='华北' AND status='paid'"),
    ("华南区域已发货的订单有多少？", "filter-region-status",
     "SELECT COUNT(*) AS cnt FROM orders WHERE region='华南' AND status='shipped'"),
    ("西南区域已完成的订单有多少？", "filter-region-status",
     "SELECT COUNT(*) AS cnt FROM orders WHERE region='西南' AND status='done'"),
    ("已取消的订单有多少？", "filter-status",
     "SELECT COUNT(*) AS cnt FROM orders WHERE status='cancelled'"),
    ("近7天创建了多少订单？", "filter-window",
     "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-08-11'"),
    ("近30天创建了多少订单？", "filter-window",
     "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19'"),
    ("近30天华东区域已支付的订单有多少？", "filter-window-region-status",
     "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19' "
     "AND region='华东' AND status='paid'"),
    ("金额最高的前5个订单的订单号和金额？", "topn",
     "SELECT order_no, amount FROM orders ORDER BY amount DESC LIMIT 5"),
    ("共有多少个不同的区域？", "distinct-count",
     "SELECT COUNT(DISTINCT region) AS cnt FROM orders"),
    ("各区域的订单数量？", "groupby",
     "SELECT region, COUNT(*) AS cnt FROM orders GROUP BY region ORDER BY region"),
    ("各状态的订单数量？", "groupby",
     "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status ORDER BY status"),
    ("近30天各区域的订单数量？", "groupby-window",
     "SELECT region, COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19' "
     "GROUP BY region ORDER BY region"),
    ("订单总金额是多少？", "agg-sum",
     "SELECT SUM(amount) AS total_amount FROM orders"),
    ("华东区域订单总金额是多少？", "agg-sum-filter",
     "SELECT SUM(amount) AS total_amount FROM orders WHERE region='华东'"),
    ("平均订单金额是多少？", "agg-avg",
     "SELECT AVG(amount) AS avg_amount FROM orders"),
    ("金额最高的前3个已完成订单的订单号和金额？", "topn-filter",
     "SELECT order_no, amount FROM orders WHERE status='done' ORDER BY amount DESC LIMIT 3"),
    ("近30天已支付的订单总金额是多少？", "agg-sum-window",
     "SELECT SUM(amount) AS total_amount FROM orders WHERE created_at >= '2026-07-19' "
     "AND status='paid'"),
    ("各区域的订单总金额？", "groupby-agg",
     "SELECT region, SUM(amount) AS total_amount FROM orders GROUP BY region ORDER BY region"),
    ("金额大于5000的订单有多少？", "filter-amount",
     "SELECT COUNT(*) AS cnt FROM orders WHERE amount > 5000"),
    ("华东区域的供应商有多少？", "filter-region",
     "SELECT COUNT(*) AS cnt FROM suppliers WHERE region='华东'"),
    ("各区域的供应商数量？", "groupby",
     "SELECT region, COUNT(*) AS cnt FROM suppliers GROUP BY region ORDER BY region"),
    ("评分最高的前5个供应商的名称和评分？", "topn",
     "SELECT name, rating FROM suppliers ORDER BY rating DESC LIMIT 5"),
    ("电子元件类目有多少商品？", "filter-category",
     "SELECT COUNT(*) AS cnt FROM products WHERE category='电子元件'"),
    ("各类目的商品数量？", "groupby",
     "SELECT category, COUNT(*) AS cnt FROM products GROUP BY category ORDER BY category"),
    ("单价最高的前3个商品的名称和单价？", "topn",
     "SELECT name, unit_price FROM products ORDER BY unit_price DESC LIMIT 3"),
    ("库存低于安全库存的商品有多少？", "filter-lowstock",
     "SELECT COUNT(*) AS cnt FROM inventory WHERE qty < safety_qty"),
    ("各仓库的库存总量？", "groupby-agg",
     "SELECT warehouse, SUM(qty) AS total_amount FROM inventory GROUP BY warehouse ORDER BY warehouse"),
    ("延迟发货的订单有多少？", "filter-delay",
     "SELECT COUNT(*) AS cnt FROM shipments WHERE delay_days > 0"),
    ("各承运商的发货数量？", "groupby",
     "SELECT carrier, COUNT(*) AS cnt FROM shipments GROUP BY carrier ORDER BY carrier"),
]

# ---------------- join 20（layer=join） ----------------

JOIN_CASES: list[tuple[str, str, str]] = [
    # (question, category, gold_sql)
    ("金额最高的前5个订单分别有多少行明细？", "join-order-items",
     "SELECT o.order_no, COUNT(i.id) AS item_cnt FROM orders o "
     "JOIN order_items i ON o.order_no=i.order_no "
     "GROUP BY o.order_no, o.amount ORDER BY o.amount DESC LIMIT 5"),
    ("电子元件类目商品的累计销量（数量）是多少？", "join-items-products",
     "SELECT SUM(i.quantity) AS total_amount FROM order_items i "
     "JOIN products p ON i.product_id=p.id WHERE p.category='电子元件'"),
    ("各类目商品的累计销售金额？", "join-groupby",
     "SELECT p.category, SUM(i.amount) AS total_amount FROM order_items i "
     "JOIN products p ON i.product_id=p.id GROUP BY p.category ORDER BY p.category"),
    ("华东区域供应商的订单总金额是多少？", "join-orders-suppliers",
     "SELECT SUM(o.amount) AS total_amount FROM orders o "
     "JOIN suppliers s ON o.supplier_id=s.id WHERE s.region='华东'"),
    ("各区域供应商的订单总金额？", "join-groupby",
     "SELECT s.region, SUM(o.amount) AS total_amount FROM orders o "
     "JOIN suppliers s ON o.supplier_id=s.id GROUP BY s.region ORDER BY s.region"),
    ("订单数量最多的前5个供应商？", "join-topn",
     "SELECT s.name, COUNT(o.id) AS cnt FROM orders o "
     "JOIN suppliers s ON o.supplier_id=s.id GROUP BY s.name ORDER BY cnt DESC LIMIT 5"),
    ("低库存商品对应的类目分布？", "join-lowstock",
     "SELECT p.category, COUNT(*) AS cnt FROM inventory i "
     "JOIN products p ON i.product_id=p.id WHERE i.qty < i.safety_qty "
     "GROUP BY p.category ORDER BY p.category"),
    ("华东仓中各类目的库存总量？", "join-inventory-products",
     "SELECT p.category, SUM(i.qty) AS total_amount FROM inventory i "
     "JOIN products p ON i.product_id=p.id WHERE i.warehouse='华东仓' "
     "GROUP BY p.category ORDER BY p.category"),
    ("延迟发货的订单分布在哪些区域？各有多少？", "join-shipments-orders",
     "SELECT o.region, COUNT(*) AS cnt FROM shipments s "
     "JOIN orders o ON s.order_no=o.order_no WHERE s.delay_days > 0 "
     "GROUP BY o.region ORDER BY o.region"),
    ("近30天延迟发货的订单有多少？", "join-window",
     "SELECT COUNT(*) AS cnt FROM shipments s "
     "JOIN orders o ON s.order_no=o.order_no "
     "WHERE s.delay_days > 0 AND o.created_at >= '2026-07-19'"),
    ("已支付订单的明细总数量是多少？", "join-items-orders",
     "SELECT SUM(i.quantity) AS total_amount FROM orders o "
     "JOIN order_items i ON o.order_no=i.order_no WHERE o.status='paid'"),
    ("已完成订单中订单金额最高的前5个供应商？", "join-topn-filter",
     "SELECT s.name, SUM(o.amount) AS total_amount FROM orders o "
     "JOIN suppliers s ON o.supplier_id=s.id WHERE o.status='done' "
     "GROUP BY s.name ORDER BY total_amount DESC LIMIT 5"),
    ("被订购次数最多的前5个商品？", "join-topn",
     "SELECT p.name, COUNT(i.id) AS cnt FROM order_items i "
     "JOIN products p ON i.product_id=p.id GROUP BY p.name ORDER BY cnt DESC LIMIT 5"),
    ("各供应商的订单平均金额最高的前5个？", "join-groupby-agg",
     "SELECT s.name, AVG(o.amount) AS avg_amount FROM orders o "
     "JOIN suppliers s ON o.supplier_id=s.id GROUP BY s.name "
     "ORDER BY avg_amount DESC LIMIT 5"),
    ("办公用品类目商品的销售总金额是多少？", "join-items-products",
     "SELECT SUM(i.amount) AS total_amount FROM order_items i "
     "JOIN products p ON i.product_id=p.id WHERE p.category='办公用品'"),
    ("延迟发货单数最多的前3个承运商？", "join-topn",
     "SELECT s.carrier, COUNT(*) AS cnt FROM shipments s "
     "JOIN orders o ON s.order_no=o.order_no WHERE s.delay_days > 0 "
     "GROUP BY s.carrier ORDER BY cnt DESC LIMIT 3"),
    ("库存量最高的前5个商品的名称和库存量？", "join-inventory-products",
     "SELECT p.name, i.qty FROM inventory i "
     "JOIN products p ON i.product_id=p.id ORDER BY i.qty DESC LIMIT 5"),
    ("华南区域供应商名下订单总金额是多少？", "join-orders-suppliers",
     "SELECT SUM(o.amount) AS total_amount FROM orders o "
     "JOIN suppliers s ON o.supplier_id=s.id WHERE s.region='华南'"),
    ("销售金额最高的商品是哪个？", "join-topn",
     "SELECT p.name, SUM(i.amount) AS total_amount FROM order_items i "
     "JOIN products p ON i.product_id=p.id GROUP BY p.name "
     "ORDER BY total_amount DESC LIMIT 1"),
    ("已完成订单中延迟发货的有多少？", "join-shipments-orders",
     "SELECT COUNT(*) AS cnt FROM shipments s "
     "JOIN orders o ON s.order_no=o.order_no WHERE o.status='done' AND s.delay_days > 0"),
]


def main() -> None:
    cases: list[dict] = []
    seq = 0
    for q, cat, sql in SINGLE_CASES:
        seq += 1
        cases.append({"id": seq, "layer": "single", "category": cat, "question": q, "gold_sql": sql})
    for q, cat, sql in JOIN_CASES:
        seq += 1
        cases.append({"id": seq, "layer": "join", "category": cat, "question": q, "gold_sql": sql})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    n_single = sum(1 for c in cases if c["layer"] == "single")
    n_join = sum(1 for c in cases if c["layer"] == "join")
    print(f"评测集已生成: {OUT}")
    print(f"  共 {len(cases)} 条: 单表 {n_single} / join {n_join}")
    from collections import Counter
    print("  类别分布:", dict(Counter(c["category"] for c in cases)))


if __name__ == "__main__":
    main()
