"""业务库 scm_biz 六表 ORM（W24 Day1）——NL2SQL 数据分析域的"靶场"数据模型。

对应《02》3.2 节 DDL +《W24学习执行手册》Day1 设计要点：
- 六表：orders / order_items / products / suppliers / inventory / shipments
- 关系链：
    orders --(order_no)--> order_items --(product_id)--> products
    orders --(supplier_id)--> suppliers
    shipments.order_no 与 orders.order_no 对应（shipped 状态才有发货记录）
- orders 核心表：ENUM 状态 + `idx_status_created` `idx_region` 复合索引
  （高频查询模式："近 N 天某状态的订单"、"按区域分组"）
- 金额勾稽：order_items.amount = quantity × unit_price；orders.amount = Σ明细
- ENUM 列用 `sa.Enum`（迁移脚本里核对 MySQL ENUM 而非 VARCHAR，手册坑）
- 毫秒时间戳沿用平台库 `_dt3()` 写法（MySQL 方言 DATETIME(fsp=3)）
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Enum,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME, DECIMAL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 固定枚举值（与 mock_biz_server 状态机一致，种子生成器保证流转合法）
REGIONS = ("华东", "华北", "华南", "西南")
ORDER_STATUSES = ("draft", "paid", "shipped", "done", "cancelled")


class BizBase(DeclarativeBase):
    """业务库声明式基类。"""


def _dt3() -> DATETIME:
    """返回 `DATETIME(3)` 类型（毫秒精度），兼容 `CURRENT_TIMESTAMP(3)` 默认值。"""
    return DATETIME(fsp=3)


class Supplier(BizBase):
    __tablename__ = "suppliers"
    __table_args__ = (Index("idx_supplier_region", "region"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_code: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, comment="SUP-XXXX"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str] = mapped_column(
        Enum(*REGIONS), nullable=False, comment="华东/华北/华南/西南"
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False, comment="评分 60–95（种子分布）")
    contact: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class Product(BizBase):
    __tablename__ = "products"
    __table_args__ = (Index("idx_product_category", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, comment="SKU-XXXXXXXX"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="类目 8 种（电子/机械/包装...）"
    )
    unit_price: Mapped[float] = mapped_column(
        DECIMAL(10, 2), nullable=False, comment="单价 10–5000"
    )
    status: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class Order(BizBase):
    __tablename__ = "orders"
    __table_args__ = (
        # ★ 核心复合索引：服务"近 N 天某状态订单"高频查询（评测 join/过滤主力）
        Index("idx_status_created", "status", "created_at"),
        Index("idx_region", "region"),
        Index("idx_supplier_created", "supplier_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(
        String(16), unique=True, nullable=False, comment="SO-YYYYMMDD-XXXX"
    )
    supplier_id: Mapped[int] = mapped_column(Integer, nullable=False)
    region: Mapped[str] = mapped_column(Enum(*REGIONS), nullable=False)
    status: Mapped[str] = mapped_column(Enum(*ORDER_STATUSES), nullable=False)
    amount: Mapped[float] = mapped_column(
        DECIMAL(12, 2), nullable=False, comment="订单总金额 = Σ明细，种子勾稽"
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


class OrderItem(BizBase):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("idx_item_order", "order_no"),
        Index("idx_item_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(16), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(DECIMAL(10, 2), nullable=False)
    amount: Mapped[float] = mapped_column(
        DECIMAL(12, 2), nullable=False, comment="quantity × unit_price，种子勾稽"
    )


class Inventory(BizBase):
    __tablename__ = "inventory"
    __table_args__ = (Index("idx_inv_product", "product_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    warehouse: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="仓库（华东仓/华北仓/...）"
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, comment="在库数量")
    safety_qty: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="安全库存（低库存评测用）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        _dt3(),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
    )


class Shipment(BizBase):
    __tablename__ = "shipments"
    __table_args__ = (
        Index("idx_ship_order", "order_no"),
        Index("idx_ship_status_shipped", "shipped_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    carrier: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="承运商（顺丰/圆通/中通/德邦...）"
    )
    tracking_no: Mapped[str] = mapped_column(String(32), nullable=False)
    shipped_at: Mapped[datetime | None] = mapped_column(_dt3(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(_dt3(), nullable=True)
    delay_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="延迟发货天数，>0 即延迟（~8% 供日报）",
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
