"""业务库 scm_biz 固定 seed 生成器（W24 Day1）——NL2SQL 靶场数据。

规模（对齐《W24学习执行手册》六问 + 《02》3.2 节）：
- suppliers  40    名称带区域词（华东/华北/华南/西南各 10）；评分 60–95
- products   500   SKU 编码 + 类目 8 种 + 单价 10–5000
- orders     10,000  `SO-YYYYMMDD-XXXX` 编号；时间分布近 180 天（近 30 天加密 40%）；
                     状态 draft5/paid20/shipped40/done30/cancelled5 流转合法；金额与明细勾稽
- order_items 30,000 每单 2–5 行；amount = quantity × unit_price
- shipments   ≈7,000 仅 shipped/done 有发货记录；延迟发货率 ~8%（供日报）
- inventory   500    每商品一条；~15% 低库存（qty < safety_qty，供评测）

设计要点（手册 Day1 坑）：
- 固定 seed：random.seed(42)，且基准日期固定 BASE_DATE（不随运行日漂移）→
  连跑两遍数据逐行一致（COUNT + 关键字段 md5 校验和比对）
- 幂等：seed 前 TRUNCATE 各表（不"先查后插"——万级数据先查后插太慢且无意义，
  reseed 语义即"drop→create→seed"）
- 只读验证：nl2sql_ro 账号 UPDATE 被拒（ERROR 1142）——见 deploy/initdb 与 day1 报告

用法：
  python -X utf8 scripts/seed_biz.py            # seed（幂等：先 TRUNCATE 再插入）
  python -X utf8 scripts/seed_biz.py --reseed   # drop 六表 → 重建 → seed（Makefile reseed-biz）
  python -X utf8 scripts/seed_biz.py --check    # 只打印行数 + 校验和（不写库）
"""

import asyncio
import hashlib
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 脚本从项目根目录跑：把 backend 加入 import path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.domains.data.models_biz import (
    BizBase,
    Inventory,
    Order,
    OrderItem,
    Product,
    Shipment,
    Supplier,
)
from app.platform.settings import settings

# ---- 固定 seed ----
RNG = random.Random(42)
BASE_DATE = date(2026, 8, 18)  # 基准日期固定 → 数据确定性（不随运行日漂移，重放一致）

# ---- 常量 ----
REGIONS = ("华东", "华北", "华南", "西南")
WAREHOUSES = ("华东仓", "华北仓", "华南仓", "西南仓")
ORDER_STATUSES = ("draft", "paid", "shipped", "done", "cancelled")
# 状态分布（手册：draft 5 / paid 20 / shipped 40 / done 30 / cancelled 5）
STATUS_WEIGHTS = {"draft": 5, "paid": 20, "shipped": 40, "done": 30, "cancelled": 5}
CATEGORIES = (
    "电子元件",
    "机械配件",
    "包装材料",
    "办公用品",
    "五金工具",
    "化工原料",
    "纺织辅料",
    "仓储设备",
)
CARRIERS = ("顺丰", "圆通", "中通", "德邦", "京东物流")
# 每类目预置一批商品名（生成 name 用）
CATEGORY_ITEMS: dict[str, list[str]] = {
    "电子元件": ["贴片电阻", "电解电容", "微控制器", "连接器", "光耦"],
    "机械配件": ["轴承", "齿轮", "密封圈", "皮带轮", "法兰盘"],
    "包装材料": ["瓦楞纸箱", "气泡膜", "缠绕膜", "封箱胶带", "防潮袋"],
    "办公用品": ["A4打印纸", "签字笔", "文件夹", "订书机", "硒鼓"],
    "五金工具": ["螺丝批", "扳手", "钢丝钳", "卷尺", "美工刀"],
    "化工原料": ["树脂", "稀释剂", "清洁剂", "润滑脂", "防锈剂"],
    "纺织辅料": ["涤纶纱线", "纯棉布", "拉链", "纽扣", "无纺布"],
    "仓储设备": ["塑料托盘", "仓储货架", "周转箱", "标签纸", "手持扫码枪"],
}

N_SUPPLIERS = 40
N_PRODUCTS = 500
N_ORDERS = 10_000
ITEMS_PER_ORDER = (2, 5)  # 每单 2–5 行明细
DELAY_RATE = 0.08  # 延迟发货率 ~8%
LOW_STOCK_RATE = 0.15  # 低库存占比 ~15%
RECENT30_WEIGHT = 0.4  # 近 30 天订单加密 40%
TOTAL_DAYS = 180


# ==================== 数据生成 ====================


def _build_suppliers() -> list[dict[str, Any]]:
    """40 供应商：华东/华北/华南/西南各 10；名称含区域词；评分 60–95。"""
    rows: list[dict[str, Any]] = []
    seq = 0
    for region in REGIONS:
        for _ in range(10):
            seq += 1
            name_words = [
                "鑫达",
                "恒远",
                "众联",
                "启航",
                "优品",
                "顺达",
                "宏图",
                "永盛",
                "利丰",
                "骏业",
            ]
            name = f"{region}{RNG.choice(name_words)}{RNG.randint(1, 99)}有限公司"
            rows.append(
                {
                    "supplier_code": f"SUP-{seq:04d}",
                    "name": name,
                    "region": region,
                    "rating": RNG.randint(60, 95),
                    "contact": f"13{RNG.randint(0, 9)}{RNG.randint(10000000, 99999999)}",
                }
            )
    return rows


def _build_products() -> list[dict[str, Any]]:
    """500 商品：SKU + 类目 8 种 + 单价 10–5000；名称与类目关联。"""
    rows: list[dict[str, Any]] = []
    for i in range(1, N_PRODUCTS + 1):
        category = RNG.choice(CATEGORIES)
        item = RNG.choice(CATEGORY_ITEMS[category])
        price = RNG.randint(10, 5000) + RNG.choice((0, 0.5, 0.8))
        rows.append(
            {
                "sku": f"SKU-{i:08d}",
                "name": f"{item} {i}号",
                "category": category,
                "unit_price": round(price, 2),
            }
        )
    return rows


def _order_date() -> date:
    """时间分布近 180 天（近 30 天加密 40%）——保证"近7天/近30天"查询有数据。"""
    if RNG.random() < RECENT30_WEIGHT:
        days_ago = RNG.randint(0, 29)
    else:
        days_ago = RNG.randint(30, TOTAL_DAYS - 1)
    return BASE_DATE - timedelta(days=days_ago)


def _pick_status() -> str:
    total = sum(STATUS_WEIGHTS.values())
    r = RNG.randint(1, total)
    acc = 0
    for status, w in STATUS_WEIGHTS.items():
        acc += w
        if r <= acc:
            return status
    return "done"  # unreachable


def _build_orders(
    suppliers: list[dict[str, Any]], products: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """订单 + 明细 + 发货 + 库存（金额勾稽 / 状态流转合法 / 发货与状态一致）。

    返回 (orders, order_items, shipments, inventories)
    """
    region_to_suppliers: dict[str, list[int]] = {}
    for s in suppliers:
        region_to_suppliers.setdefault(s["region"], []).append(s["supplier_code"])

    # 按日期维护订单号序号 → 保证 SO-YYYYMMDD-XXXX 全局唯一
    day_seq: dict[str, int] = {}
    price_by_product: dict[str, float] = {}
    for p in products:
        price_by_product[p["sku"]] = p["unit_price"]
    product_ids = [p["sku"] for p in products]

    orders: list[dict[str, Any]] = []
    order_items: list[dict[str, Any]] = []
    shipments: list[dict[str, Any]] = []
    inventories: list[dict[str, Any]] = []

    for _ in range(N_ORDERS):
        d = _order_date()
        day_key = d.strftime("%Y%m%d")
        seq = day_seq.get(day_key, 0) + 1
        day_seq[day_key] = seq
        order_no = f"SO-{day_key}-{seq:04d}"

        status = _pick_status()
        # 订单区域：与供应商区域关联（80% 取供应商区域，增强 join 查询语义）
        region = RNG.choices(REGIONS, weights=(35, 25, 25, 15))[0]
        if RNG.random() < 0.8 and region_to_suppliers.get(region):
            supplier_code = RNG.choice(region_to_suppliers[region])
        else:
            supplier_code = RNG.choice(suppliers)["supplier_code"]

        # 明细：每单 2–5 行，金额勾稽
        n_items = RNG.randint(*ITEMS_PER_ORDER)
        chosen = RNG.sample(product_ids, n_items)
        total = 0.0
        for sku in chosen:
            qty = RNG.randint(1, 50)
            unit_price = price_by_product[sku]
            amount = round(qty * unit_price, 2)
            total += amount
            order_items.append(
                {
                    "order_no": order_no,
                    "product_id": sku,  # 占位：插入前映射为 int
                    "product_sku": sku,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "amount": amount,
                }
            )

        remark = RNG.choice([None, None, None, "加急", "客户指定承运商", "分批到货"])
        orders.append(
            {
                "order_no": order_no,
                "supplier_id": supplier_code,  # 占位：插入前映射为 int
                "supplier_code_ref": supplier_code,
                "region": region,
                "status": status,
                "amount": round(total, 2),
                "created_at": _created_at(d, status),
                "remark": remark,
            }
        )

        # 发货：仅 shipped/done（= 40%+30% = 70% → ≈7000 行）
        if status in ("shipped", "done"):
            shipments.append(_build_shipment(order_no, d, status))

    # 库存：每商品一条，~15% 低库存
    for p in products:
        qty = RNG.randint(0, 800)
        safety = RNG.randint(20, 100)
        if RNG.random() < LOW_STOCK_RATE:
            qty = RNG.randint(0, max(0, safety - 1))  # 保证 qty < safety_qty
        inventories.append(
            {
                "product_id": p["sku"],  # 占位
                "product_sku": p["sku"],
                "warehouse": RNG.choice(WAREHOUSES),
                "qty": qty,
                "safety_qty": safety,
            }
        )

    return orders, order_items, shipments, inventories


def _created_at(d: date, status: str) -> datetime:
    """订单创建时间：当天随机时分；cancelled 略早（取消单时间戳分布一致即可）。"""
    hour, minute, second = RNG.randint(0, 23), RNG.randint(0, 59), RNG.randint(0, 59)
    return datetime(d.year, d.month, d.day, hour, minute, second)


def _build_shipment(order_no: str, order_date: date, status: str) -> dict[str, Any]:
    """发货记录：发货时间在订单后 0–5 天；~8% 延迟（delay_days 1–15）。

    done 订单有 delivered_at（发货后 1–7 天签收）；shipped 无。
    """
    delayed = RNG.random() < DELAY_RATE
    ship_days = RNG.randint(0, 5) + (RNG.randint(1, 15) if delayed else 0)
    shipped_at = datetime(
        order_date.year, order_date.month, order_date.day, RNG.randint(8, 20), RNG.randint(0, 59)
    ) + timedelta(days=ship_days)
    delay_days = RNG.randint(1, 15) if delayed else 0
    delivered_at = None
    if status == "done":
        delivered_at = shipped_at + timedelta(days=RNG.randint(1, 7))
    return {
        "order_no": order_no,
        "carrier": RNG.choice(CARRIERS),
        "tracking_no": f"{RNG.choice(CARRIERS)}{RNG.randint(1000000000, 9999999999)}",
        "shipped_at": shipped_at,
        "delivered_at": delivered_at,
        "delay_days": delay_days,
        "remark": "延迟发货" if delayed else None,
    }


# ==================== 入库 ====================


async def _truncate(conn) -> None:
    await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    for table in ("suppliers", "products", "orders", "order_items", "shipments", "inventory"):
        await conn.execute(text(f"TRUNCATE TABLE {table}"))
    await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


async def _insert(conn, table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    # 分批 executemany（5000/批）
    batch = 5000
    cols = list(rows[0].keys())
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        sql = text(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
        )
        await conn.execute(sql, [dict(r) for r in chunk])


async def seed(dsn: str, reseed: bool) -> None:
    engine = create_async_engine(dsn, pool_pre_ping=True)
    async with engine.begin() as conn:
        if reseed:
            await conn.run_sync(BizBase.metadata.drop_all)
            await conn.run_sync(BizBase.metadata.create_all)
        await _truncate(conn)

        # 生成数据（固定 seed，完全确定性）
        suppliers = _build_suppliers()
        products = _build_products()
        orders, order_items, shipments, inventories = _build_orders(suppliers, products)

        # 占位 → 实际 id 映射
        supplier_id = {r["supplier_code"]: i + 1 for i, r in enumerate(suppliers)}
        product_id = {r["sku"]: i + 1 for i, r in enumerate(products)}

        # 确定性 created_at：固定基准（BASE_DATE 00:00:00 起按 id 递增微偏移），
        # 保证连跑两遍逐行完全一致（不依赖 MySQL CURRENT_TIMESTAMP 的运行时刻）
        sup_rows = [
            {
                "supplier_code": r["supplier_code"],
                "name": r["name"],
                "region": r["region"],
                "rating": r["rating"],
                "contact": r["contact"],
                "created_at": datetime(BASE_DATE.year, BASE_DATE.month, BASE_DATE.day)
                + timedelta(minutes=i),
            }
            for i, r in enumerate(suppliers)
        ]
        prod_rows = [
            {
                "sku": r["sku"],
                "name": r["name"],
                "category": r["category"],
                "unit_price": r["unit_price"],
                "created_at": datetime(BASE_DATE.year, BASE_DATE.month, BASE_DATE.day)
                + timedelta(minutes=i),
            }
            for i, r in enumerate(products)
        ]
        order_rows = [
            {
                "order_no": r["order_no"],
                "supplier_id": supplier_id[r["supplier_code_ref"]],
                "region": r["region"],
                "status": r["status"],
                "amount": r["amount"],
                "remark": r["remark"],
                "created_at": r["created_at"],
            }
            for r in orders
        ]
        item_rows = [
            {
                "order_no": r["order_no"],
                "product_id": product_id[r["product_sku"]],
                "quantity": r["quantity"],
                "unit_price": r["unit_price"],
                "amount": r["amount"],
            }
            for r in order_items
        ]
        ship_rows = [
            {
                "order_no": r["order_no"],
                "carrier": r["carrier"],
                "tracking_no": r["tracking_no"],
                "shipped_at": r["shipped_at"],
                "delivered_at": r["delivered_at"],
                "delay_days": r["delay_days"],
                "remark": r["remark"],
            }
            for r in shipments
        ]
        inv_rows = [
            {
                "product_id": product_id[r["product_sku"]],
                "warehouse": r["warehouse"],
                "qty": r["qty"],
                "safety_qty": r["safety_qty"],
            }
            for r in inventories
        ]

        await _insert(conn, "suppliers", sup_rows)
        await _insert(conn, "products", prod_rows)
        await _insert(conn, "orders", order_rows)
        await _insert(conn, "order_items", item_rows)
        await _insert(conn, "shipments", ship_rows)
        await _insert(conn, "inventory", inv_rows)

        print(
            f"  插入完成: suppliers={len(sup_rows)} products={len(prod_rows)} "
            f"orders={len(order_rows)} items={len(item_rows)} "
            f"shipments={len(ship_rows)} inventory={len(inv_rows)}"
        )
    await engine.dispose()


async def check(dsn: str) -> None:
    """行数 + 关键字段 md5 校验和（重放一致性验收）。

    注意：GROUP_CONCAT 默认 1MB 上限，34934 行 order_items 会被截断产生假校验和，
    先 SET SESSION group_concat_max_len 放大（校验和仅供重放一致性比对，session 级不影响业务）。
    """
    engine = create_async_engine(dsn, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.execute(text("SET SESSION group_concat_max_len = 100000000"))
        print("== 行数 + 校验和 ==")
        for table, cols in (
            ("suppliers", ["supplier_code", "region", "rating"]),
            ("products", ["sku", "category", "unit_price"]),
            ("orders", ["order_no", "region", "status", "amount"]),
            ("order_items", ["order_no", "product_id", "quantity", "amount"]),
            ("shipments", ["order_no", "carrier", "delay_days"]),
            ("inventory", ["product_id", "warehouse", "qty", "safety_qty"]),
        ):
            cnt = await conn.scalar(text(f"SELECT COUNT(*) FROM {table}"))
            concat_expr = "CONCAT_WS('|', " + ", ".join(cols) + ")"
            checksum = await conn.scalar(
                text(f"SELECT MD5(GROUP_CONCAT({concat_expr} ORDER BY id)) FROM {table}")
            )
            print(f"  {table:12s} rows={cnt:>6d}  md5={checksum}")
    await engine.dispose()


async def main() -> None:
    reseed = "--reseed" in sys.argv
    check_only = "--check" in sys.argv
    dsn = settings.biz_dsn
    print(f"scm_biz seed @ {dsn}")
    if check_only:
        await check(dsn)
        return
    await seed(dsn, reseed=reseed)
    print("Seed 完成（幂等：TRUNCATE 后重插，连跑两遍结果一致）")
    await check(dsn)


if __name__ == "__main__":
    asyncio.run(main())
