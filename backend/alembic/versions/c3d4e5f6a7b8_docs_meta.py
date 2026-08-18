"""docs_meta table (W25 Day2 kb_increment_sync 增量同步的文档元数据登记表)

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-08-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "docs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("doc_id", sa.String(length=128), nullable=False),
        sa.Column("file", sa.String(length=255), nullable=False, comment="文件名（md）"),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("topic", sa.String(length=32), nullable=True),
        sa.Column(
            "file_mtime",
            mysql.DATETIME(fsp=3),
            nullable=False,
            comment="文件系统 mtime（增量检测水位）",
        ),
        sa.Column(
            "content_hash", sa.String(length=64), nullable=False, comment="文件内容 sha256（防 mtime 精度漏检）"
        ),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="active / deleted",
        ),
        sa.Column(
            "first_seen_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("doc_id"),
    )
    op.create_index("idx_docs_mtime", "docs", ["file_mtime"], unique=False)
    op.create_index("idx_docs_status", "docs", ["status"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_docs_status", table_name="docs")
    op.drop_index("idx_docs_mtime", table_name="docs")
    op.drop_table("docs")
