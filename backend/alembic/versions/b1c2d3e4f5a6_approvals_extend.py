"""approvals 表扩展（W23 Day5 审批服务迁 MySQL）

审批单运行时落库需要保留业务元信息（human-readable 操作描述 / 审批理由 /
幂等键），现有表只有 action/target/diff 字段。新增三列补齐，保证
ApprovalService 从 sqlite 切 MySQL 后接口语义无损。

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-08-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("approvals", sa.Column("operation", sa.String(length=64), nullable=True,
                                         comment="人类可读操作描述（修改订单/取消订单）"))
    op.add_column("approvals", sa.Column("reason", sa.Text(), nullable=True,
                                         comment="审批理由（diff 摘要/用户说明）"))
    op.add_column("approvals", sa.Column("idem_key", sa.String(length=64), nullable=True,
                                         comment="审批发起时生成的幂等键"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("approvals", "idem_key")
    op.drop_column("approvals", "reason")
    op.drop_column("approvals", "operation")
