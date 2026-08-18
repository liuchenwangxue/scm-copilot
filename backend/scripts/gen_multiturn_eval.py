"""多轮指代消解评测集生成器（W24 Day5）——10 条对话（每条 2–3 轮），固定 gold SQL。

对应《W24学习执行手册》Day5 下午：
- 每条用例 = 一段连续对话（turns），每轮含：
    q         本轮用户问题（首轮即完整问题，后续为带省略/指代的追问）
    resolved  指代消解后的完整问题（mock 规则消解的目标字符串；real 以其为消解质量参照）
    gold_sql  该轮完整问题对应的标准 SQL（固定 seed 数据，结果集即金标准）
- 追问覆盖：区域替换（华东→华北/华南/西南）、时间替换/补插（近7天↔近30天）、
  状态替换/补插（已取消→已完成 / 补插"已支付"）、区域→各区域（聚合对比）、
  状态+时间组合（只看已完成 → 近30天）——即手册示例"上个月华东的延迟订单"→"那华南呢？"同型。

输出：backend/evals/multiturn_eval.jsonl（每行一条 JSON：{id, turns: [...]}）

用法：python -X utf8 backend/scripts/gen_multiturn_eval.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "evals" / "multiturn_eval.jsonl"

# 每条：{id, turns: [(q, resolved, gold_sql), ...]}（T0 必为完整问题）
CASES: list[list[tuple[str, str, str]]] = [
    # 1. 时间窗替换
    [
        ("近7天创建了多少订单？", "近7天创建了多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-08-11'"),
        ("那近30天呢？", "近30天创建了多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19'"),
    ],
    # 2. 区域 → 各区域（单区域聚合 → 全区域分组）
    [
        ("华东区域订单总金额是多少？", "华东区域订单总金额是多少？",
         "SELECT SUM(amount) AS total_amount FROM orders WHERE region='华东'"),
        ("那各区域呢？", "各区域订单总金额是多少？",
         "SELECT region, SUM(amount) AS total_amount FROM orders GROUP BY region ORDER BY region"),
    ],
    # 3. 区域替换（带时间+状态限定延续）
    [
        ("近30天华东区域已支付的订单有多少？", "近30天华东区域已支付的订单有多少？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19' "
         "AND region='华东' AND status='paid'"),
        ("那华北呢？", "近30天华北区域已支付的订单有多少？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19' "
         "AND region='华北' AND status='paid'"),
    ],
    # 4. 连续区域替换（三轮）
    [
        ("华东区域有多少订单？", "华东区域有多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE region='华东'"),
        ("华北呢？", "华北区域有多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE region='华北'"),
        ("华南呢？", "华南区域有多少订单？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE region='华南'"),
    ],
    # 5. 状态替换
    [
        ("已取消的订单有多少？", "已取消的订单有多少？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE status='cancelled'"),
        ("那已完成的呢？", "已完成的订单有多少？",
         "SELECT COUNT(*) AS cnt FROM orders WHERE status='done'"),
    ],
    # 6. 时间补插（前轮无时间条件）
    [
        ("延迟发货的订单有多少？", "延迟发货的订单有多少？",
         "SELECT COUNT(*) AS cnt FROM shipments WHERE delay_days > 0"),
        ("那近30天呢？", "近30天延迟发货的订单有多少？",
         "SELECT COUNT(*) AS cnt FROM shipments s JOIN orders o ON s.order_no=o.order_no "
         "WHERE s.delay_days > 0 AND o.created_at >= '2026-07-19'"),
    ],
    # 7. 状态补插（保留时间+区域分组）
    [
        ("近30天各区域的订单数量？", "近30天各区域的订单数量？",
         "SELECT region, COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19' "
         "GROUP BY region ORDER BY region"),
        ("只算已支付的呢？", "近30天各区域的已支付的订单数量？",
         "SELECT region, COUNT(*) AS cnt FROM orders WHERE created_at >= '2026-07-19' "
         "AND status='paid' GROUP BY region ORDER BY region"),
    ],
    # 8. 区域替换（供应商维度）
    [
        ("华东区域的供应商有多少？", "华东区域的供应商有多少？",
         "SELECT COUNT(*) AS cnt FROM suppliers WHERE region='华东'"),
        ("那华南呢？", "华南区域的供应商有多少？",
         "SELECT COUNT(*) AS cnt FROM suppliers WHERE region='华南'"),
    ],
    # 9. 各区域 → 单区域
    [
        ("各区域的订单数量？", "各区域的订单数量？",
         "SELECT region, COUNT(*) AS cnt FROM orders GROUP BY region ORDER BY region"),
        ("西南呢？", "西南区域的订单数量？",
         "SELECT region, COUNT(*) AS cnt FROM orders WHERE region='西南' "
         "GROUP BY region ORDER BY region"),
    ],
    # 10. 状态补插 + 时间补插（三轮组合）
    [
        ("金额最高的前5个订单的订单号和金额？", "金额最高的前5个订单的订单号和金额？",
         "SELECT order_no, amount FROM orders ORDER BY amount DESC LIMIT 5"),
        ("只看已完成的呢？", "金额最高的前5个已完成的订单的订单号和金额？",
         "SELECT order_no, amount FROM orders WHERE status='done' ORDER BY amount DESC LIMIT 5"),
        ("近30天呢？", "近30天金额最高的前5个已完成的订单的订单号和金额？",
         "SELECT order_no, amount FROM orders WHERE status='done' "
         "AND created_at >= '2026-07-19' ORDER BY amount DESC LIMIT 5"),
    ],
]


def main() -> None:
    records: list[dict] = []
    for idx, turns in enumerate(CASES, 1):
        records.append(
            {
                "id": idx,
                "turns": [
                    {"q": q, "resolved": resolved, "gold_sql": gold}
                    for q, resolved, gold in turns
                ],
            }
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for c in records:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    total_turns = sum(len(c["turns"]) for c in records)
    print(f"多轮评测集已生成: {OUT}")
    print(f"  共 {len(records)} 条对话 / {total_turns} 轮（首轮不计追问）")
    print("  追问轮", total_turns - len(records), "条（区域/时间/状态替换与补插）")


if __name__ == "__main__":
    main()
