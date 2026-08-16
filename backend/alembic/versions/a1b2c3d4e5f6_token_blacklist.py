"""token_blacklist table (W23 Day3 logout 吊销名单落库版)

Revision ID: a1b2c3d4e5f6
Revises: 9e14aff7d28e
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9e14aff7d28e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "token_blacklist",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False, comment="JWT ID"),
        sa.Column("token_type", sa.String(length=16), nullable=False, comment="access / refresh"),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "expires_at",
            mysql.DATETIME(fsp=3),
            nullable=False,
            comment="吊销截至",
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jti"),
    )
    op.create_index("idx_blacklist_jti_expires", "token_blacklist", ["jti", "expires_at"], unique=False)
    op.create_index("idx_blacklist_created", "token_blacklist", ["created_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_blacklist_created", table_name="token_blacklist")
    op.drop_index("idx_blacklist_jti_expires", table_name="token_blacklist")
    op.drop_table("token_blacklist")
