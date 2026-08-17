"""内存数据存储 + 种子数据（W19 Day2，W23 Day6 随 SCM Copilot 部署复制）。

3 张表：orders（20 条，覆盖 5 状态）/ inventory（15 项，含 3 个低库存）/ suppliers（5 家）。
数据为模块级 dict，服务进程内可读可写（写操作 PATCH/cancel 修改内存，重启即恢复种子）。
"""
from copy import deepcopy

# ---- 订单状态机（★业务规则真实）----
# 草稿 → 审批中 → 已下单 → 已发货 → 已关闭（单向流转，不可回退）
ORDER_STATUS = {
    "draft": "草稿",
    "approving": "审批中",
    "ordered": "已下单",
    "shipped": "已发货",
    "closed": "已关闭",
}
# 可修改（PATCH 金额/交期）的状态：shipped/closed 不可改
EDITABLE_STATUSES = ("draft", "approving", "ordered")
# 可取消的状态：shipped（需退货流程）/closed（终态）不可取消
CANCELLABLE_STATUSES = ("draft", "approving", "ordered")


def _order(order_id, status, amount, delivery_date, supplier_id, created_at):
    """构造订单记录（updated_at = created_at + 3 天，模拟流转留痕）。"""
    return {
        "order_id": order_id,
        "status": status,
        "status_label": ORDER_STATUS[status],
        "amount": round(float(amount), 2),
        "delivery_date": delivery_date,
        "supplier_id": supplier_id,
        "created_at": created_at,
        "updated_at": _plus_days(created_at, 3),
    }


def _plus_days(iso_date: str, days: int) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(iso_date) + timedelta(days=days)
    return f"{d.isoformat()}T12:00:00Z"


# ---- 种子数据 ----
_ORDERS_SEED = [
    # 草稿 ×4
    ("PO-0001", "draft", 12500.00, "2026-09-10", "SUP-001", "2026-08-01"),
    ("PO-0002", "draft", 8900.50, "2026-09-15", "SUP-002", "2026-08-02"),
    ("PO-0003", "draft", 45600.00, "2026-09-20", "SUP-003", "2026-08-04"),
    ("PO-0004", "draft", 2300.00, "2026-08-30", "SUP-004", "2026-08-05"),
    # 审批中 ×4
    ("PO-0005", "approving", 67800.00, "2026-10-01", "SUP-001", "2026-08-06"),
    ("PO-0006", "approving", 15000.00, "2026-09-25", "SUP-005", "2026-08-07"),
    ("PO-0007", "approving", 33200.00, "2026-10-10", "SUP-002", "2026-08-08"),
    ("PO-0008", "approving", 9800.00, "2026-09-05", "SUP-003", "2026-08-09"),
    # 已下单 ×5
    ("PO-0009", "ordered", 125000.00, "2026-09-30", "SUP-001", "2026-07-20"),
    ("PO-0010", "ordered", 45600.00, "2026-09-12", "SUP-002", "2026-07-22"),
    ("PO-0011", "ordered", 7800.00, "2026-08-28", "SUP-004", "2026-07-25"),
    ("PO-0012", "ordered", 23400.00, "2026-10-05", "SUP-005", "2026-07-28"),
    ("PO-0013", "ordered", 56700.00, "2026-09-18", "SUP-003", "2026-08-01"),
    # 已发货 ×5
    ("PO-0014", "shipped", 89000.00, "2026-08-10", "SUP-001", "2026-07-05"),
    ("PO-0015", "shipped", 34500.00, "2026-08-12", "SUP-002", "2026-07-08"),
    ("PO-0016", "shipped", 12800.00, "2026-08-15", "SUP-003", "2026-07-10"),
    ("PO-0017", "shipped", 6700.00, "2026-08-14", "SUP-004", "2026-07-12"),
    ("PO-0018", "shipped", 92300.00, "2026-08-18", "SUP-005", "2026-07-15"),
    # 已关闭 ×2
    ("PO-0019", "closed", 56000.00, "2026-07-30", "SUP-001", "2026-06-20"),
    ("PO-0020", "closed", 18900.00, "2026-07-25", "SUP-002", "2026-06-25"),
]

_SUPPLIERS = {
    "SUP-001": {"supplier_id": "SUP-001", "name": "华东精密制造", "level": "A",
                "contact": "王经理", "phone": "021-5550-1001", "payment_terms_days": 45},
    "SUP-002": {"supplier_id": "SUP-002", "name": "华南金属材料", "level": "A",
                "contact": "李总", "phone": "0755-5550-2002", "payment_terms_days": 45},
    "SUP-003": {"supplier_id": "SUP-003", "name": "西部包装实业", "level": "B",
                "contact": "张工", "phone": "029-5550-3003", "payment_terms_days": 30},
    "SUP-004": {"supplier_id": "SUP-004", "name": "北方电子元件", "level": "B",
                "contact": "赵经理", "phone": "010-5550-4004", "payment_terms_days": 30},
    "SUP-005": {"supplier_id": "SUP-005", "name": "中部化工原料", "level": "C",
                "contact": "陈工", "phone": "027-5550-5005", "payment_terms_days": 15},
}

# (sku, name, category, qty, safety_stock, unit_price, supplier_id)
_INVENTORY_SEED = [
    ("SKU-001", "304 不锈钢管件", "原材料", 120, 50, 8.50, "SUP-001"),
    ("SKU-002", "铝合金板材", "原材料", 80, 60, 15.20, "SUP-002"),
    ("SKU-003", "聚乙烯颗粒", "原材料", 200, 100, 6.80, "SUP-005"),
    ("SKU-004", "伺服电机", "机电件", 35, 20, 890.00, "SUP-004"),
    ("SKU-005", "减速机", "机电件", 18, 15, 1200.00, "SUP-004"),
    ("SKU-006", "轴承组件", "机电件", 45, 30, 65.00, "SUP-004"),
    ("SKU-007", "工业传感器", "机电件", 12, 20, 320.00, "SUP-004"),   # 低库存
    ("SKU-008", "标准托盘", "包装耗材", 300, 150, 45.00, "SUP-003"),
    ("SKU-009", "瓦楞纸箱", "包装耗材", 500, 300, 2.50, "SUP-003"),
    ("SKU-010", "缠绕膜", "包装耗材", 150, 80, 18.00, "SUP-003"),
    ("SKU-011", "密封圈", "标准件", 8, 25, 1.20, "SUP-002"),          # 低库存
    ("SKU-012", "高强度螺栓", "标准件", 1000, 500, 0.35, "SUP-002"),
    ("SKU-013", "快接头", "标准件", 60, 40, 12.00, "SUP-001"),
    ("SKU-014", "液压油 32#", "辅料", 5, 10, 85.00, "SUP-005"),       # 低库存
    ("SKU-015", "防锈剂", "辅料", 40, 20, 30.00, "SUP-005"),
]

# ---- 运行时数据（进程内可变）----
orders: dict[str, dict] = {}
inventory: list[dict] = []
suppliers: dict[str, dict] = {}


def init_data() -> None:
    """（重）初始化种子数据——服务启动时调用。"""
    global orders, inventory
    orders = {o["order_id"]: o for o in (_order(*r) for r in _ORDERS_SEED)}
    inventory = [
        {"sku": s[0], "name": s[1], "category": s[2], "qty": s[3],
         "safety_stock": s[4], "unit_price": s[5], "supplier_id": s[6]}
        for s in _INVENTORY_SEED
    ]
    suppliers.update(_SUPPLIERS)


def _enrich(order: dict) -> dict:
    """订单返回前拼接供应商名称（契约 Order 字段含 supplier_name）。"""
    o = deepcopy(order)
    o["supplier_name"] = suppliers.get(o["supplier_id"], {}).get("name")
    return o


def get_order(order_id: str):
    o = orders.get(order_id)
    return _enrich(o) if o else None


def list_orders(status: str | None = None) -> list[dict]:
    items = [_enrich(o) for o in orders.values()]
    if status is not None:
        items = [o for o in items if o["status"] == status]
    # 保持种子顺序（order_id 数字序）
    items.sort(key=lambda o: int(o["order_id"].split("-")[1]))
    return items


def update_order(order_id: str, amount=None, delivery_date=None) -> dict:
    """更新订单字段并刷新 updated_at，返回更新后的订单。"""
    from datetime import datetime, timezone
    o = orders[order_id]
    if amount is not None:
        o["amount"] = round(float(amount), 2)
    if delivery_date is not None:
        o["delivery_date"] = delivery_date
    o["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return deepcopy(o)


def cancel_order(order_id: str) -> dict:
    """取消订单 → closed，返回更新后的订单。"""
    from datetime import datetime, timezone
    o = orders[order_id]
    o["status"] = "closed"
    o["status_label"] = ORDER_STATUS["closed"]
    o["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return deepcopy(o)


def list_inventory() -> list[dict]:
    rows = []
    for item in inventory:
        row = dict(item)
        row["supplier_name"] = suppliers[item["supplier_id"]]["name"]
        row["low_stock"] = item["qty"] < item["safety_stock"]
        rows.append(row)
    return rows


def reconciliation(from_date: str | None, to_date: str | None) -> list[dict]:
    """对账：按供应商聚合 ordered/shipped 订单金额（created_at 在 [from, to] 内）。

    返回 (rows, order_count, total_amount)；rows 按 total_amount 降序。
    """
    from collections import defaultdict
    agg = defaultdict(lambda: {"order_count": 0, "total_amount": 0.0})
    for o in orders.values():
        if o["status"] not in ("ordered", "shipped"):
            continue
        if from_date and o["created_at"][:10] < from_date:
            continue
        if to_date and o["created_at"][:10] > to_date:
            continue
        a = agg[o["supplier_id"]]
        a["order_count"] += 1
        a["total_amount"] += o["amount"]
    rows = [
        {"supplier_id": sid, "supplier_name": suppliers[sid]["name"],
         "order_count": v["order_count"], "total_amount": round(v["total_amount"], 2)}
        for sid, v in agg.items()
    ]
    rows.sort(key=lambda r: r["total_amount"], reverse=True)
    total_amount = round(sum(r["total_amount"] for r in rows), 2)
    order_count = sum(r["order_count"] for r in rows)
    return rows, order_count, total_amount
