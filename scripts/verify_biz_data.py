"""scm_biz 数据质量验证脚本（W24 Day1）——种子的业务真实性检查。

对齐《W24学习执行手册》Day1 验收 + 坑：
- 近 30 天 / 近 7 天必须有数据（空结果集会被评测误判为 SQL 错）
- 状态分布合法（draft 5 / paid 20 / shipped 40 / done 30 / cancelled 5）
- 金额勾稽：orders.amount = Σ order_items.amount（0 不符）
- 延迟发货率 ~8%（供 daily_brief 日报）
- 低库存占比 ~15%（qty < safety_qty，供评测）
- 发货记录仅 shipped/done 状态订单有
- 每单明细 2–5 行

用法：python -X utf8 scripts/verify_biz_data.py
输出：各指标实测值 + PASS/FAIL
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.platform.settings import settings

# 指标阈值
RECENT30_MIN = 1000  # 近 30 天订单（10,000 × 40% ≈ 4000，留余量）
RECENT7_MIN = 300  # 近 7 天订单
STATUS_EXPECT = {"draft": 0.05, "paid": 0.20, "shipped": 0.40, "done": 0.30, "cancelled": 0.05}
DELAY_EXPECT = 0.08  # 延迟率 ±3pp
LOW_STOCK_EXPECT = 0.15  # 低库存占比 ±5pp
AMOUNT_MISMATCH_MAX = 0  # 金额勾稽不符必须为 0
ITEMS_RANGE = (2, 5)  # 每单明细行数范围


async def main() -> None:
    engine = create_async_engine(settings.biz_dsn, pool_pre_ping=True)
    results: list[tuple[str, str, str]] = []  # (检查项, 实测, 判定)

    async with engine.connect() as conn:
        # ---- 1. 近 30 天 / 近 7 天数据（基准日期 seed，窗口相对 BASE_DATE）----
        base = date(2026, 8, 18)
        for name, days, lo in (
            ("近30天订单", 30, RECENT30_MIN),
            ("近7天订单", 7, RECENT7_MIN),
        ):
            cutoff = base - timedelta(days=days)
            cnt = await conn.scalar(
                text("SELECT COUNT(*) FROM orders WHERE created_at >= :cutoff"),
                {"cutoff": cutoff.isoformat()},
            )
            ok = int(cnt) >= lo
            results.append((name, f"rows={cnt} (>= {lo})", "PASS" if ok else "FAIL"))

        # ---- 2. 状态分布 ----
        rows = (
            await conn.execute(text("SELECT status, COUNT(*) AS c FROM orders GROUP BY status"))
        ).all()
        total = sum(r[1] for r in rows)
        dist = {r[0]: r[1] / total for r in rows}
        status_ok = True
        detail = []
        for st, expected in STATUS_EXPECT.items():
            actual = dist.get(st, 0.0)
            detail.append(f"{st}={actual:.1%}(exp {expected:.0%})")
            if abs(actual - expected) > 0.03:
                status_ok = False
        results.append(("状态分布", " ".join(detail), "PASS" if status_ok else "FAIL"))

        # ---- 3. 金额勾稽 ----
        mism = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM orders o WHERE o.amount <> "
                "(SELECT ROUND(SUM(amount),2) FROM order_items i WHERE i.order_no = o.order_no)"
            )
        )
        results.append(("金额勾稽", f"mismatch={mism}", "PASS" if int(mism) == 0 else "FAIL"))

        # ---- 4. 延迟发货率 ----
        ship_total, ship_delayed = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS t, SUM(CASE WHEN delay_days>0 THEN 1 ELSE 0 END) AS d "
                    "FROM shipments"
                )
            )
        ).one()
        ship_total = int(ship_total)
        ship_delayed = int(ship_delayed or 0)
        delay_rate = ship_delayed / ship_total if ship_total else 0
        delay_ok = abs(delay_rate - DELAY_EXPECT) <= 0.03
        results.append(
            (
                "延迟发货率",
                f"{delay_rate:.1%} ({ship_delayed}/{ship_total})",
                "PASS" if delay_ok else "FAIL",
            )
        )

        # ---- 5. 低库存占比 ----
        inv_total, inv_low = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS t, SUM(CASE WHEN qty<safety_qty THEN 1 ELSE 0 END) AS l "
                    "FROM inventory"
                )
            )
        ).one()
        inv_total = int(inv_total)
        inv_low = int(inv_low or 0)
        low_rate = inv_low / inv_total if inv_total else 0
        low_ok = abs(low_rate - LOW_STOCK_EXPECT) <= 0.05
        results.append(
            ("低库存占比", f"{low_rate:.1%} ({inv_low}/{inv_total})", "PASS" if low_ok else "FAIL")
        )

        # ---- 6. 发货记录仅 shipped/done 有 ----
        bad_ship = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM shipments s "
                "JOIN orders o ON s.order_no=o.order_no WHERE o.status NOT IN ('shipped','done')"
            )
        )
        results.append(
            (
                "发货↔状态一致性",
                f"非法发货记录={bad_ship}",
                "PASS" if int(bad_ship) == 0 else "FAIL",
            )
        )

        # ---- 7. 每单明细行数 2–5 ----
        bad_items = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM (SELECT order_no, COUNT(*) c FROM order_items "
                "GROUP BY order_no HAVING c < :lo OR c > :hi) t"
            ),
            {"lo": ITEMS_RANGE[0], "hi": ITEMS_RANGE[1]},
        )
        results.append(
            ("明细行数 2–5", f"越界订单数={bad_items}", "PASS" if int(bad_items) == 0 else "FAIL")
        )

        # ---- 8. 供应商区域分布（华东/华北/华南/西南各 10）----
        sup = (
            await conn.execute(text("SELECT region, COUNT(*) c FROM suppliers GROUP BY region"))
        ).all()
        sup_ok = all(r[1] == 10 for r in sup)
        results.append(
            (
                "供应商区域分布",
                " ".join(f"{r[0]}={r[1]}" for r in sup),
                "PASS" if sup_ok and len(sup) == 4 else "FAIL",
            )
        )

    await engine.dispose()

    # ---- 输出 ----
    print("== scm_biz 数据质量验证 ==")
    all_pass = True
    for name, actual, verdict in results:
        print(f"  {name:16s} {actual:40s} {verdict}")
        if verdict == "FAIL":
            all_pass = False
    print("== 结果:", "ALL PASS ✅" if all_pass else "HAS FAIL ❌", "==")


if __name__ == "__main__":
    asyncio.run(main())
