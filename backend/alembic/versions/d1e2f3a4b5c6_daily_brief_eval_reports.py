"""daily_briefs / eval_reports / notifications tables (W25 Day3 调度产物落库)

Revision ID: d1e2f3a4b5c6
Revises: c3d4e5f6a7b8
Create Date: 2026-09-02

三张表对应《W25学习执行手册》Day3：
- daily_briefs：经营日报（brief_date 唯一，SQL 可回溯 + 订阅推送）
- eval_reports：夜间质量回归（(report_date, domain) 唯一，7 日均值偏离标红）
- notifications：站内通知（订阅推送落点，非目标清单不接邮件/IM）
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "daily_briefs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "brief_date",
            sa.String(length=10),
            nullable=False,
            comment="日报归属日 YYYY-MM-DD（唯一）",
        ),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True, comment="GMV/延迟率/TOP5"),
        sa.Column("sqls", sa.JSON(), nullable=True, comment="三条 SQL 原文+结果（可回溯）"),
        sa.Column("notified_users", sa.JSON(), nullable=True, comment="推送用户列表"),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'generated'"),
            nullable=False,
            comment="generated / pushed",
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("brief_date"),
    )
    op.create_index("idx_daily_brief_date", "daily_briefs", ["brief_date"], unique=False)

    op.create_table(
        "eval_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("report_date", sa.String(length=10), nullable=False, comment="报告归属日"),
        sa.Column("domain", sa.String(length=16), nullable=False, comment="rag / nl2sql"),
        sa.Column("metrics", sa.JSON(), nullable=True, comment="各域指标"),
        sa.Column("deviation", sa.JSON(), nullable=True, comment="与7日均值偏离"),
        sa.Column(
            "regressed",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="1=劣化>5pp 标红",
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_date", "domain"),
    )
    op.create_index(
        "idx_eval_report_domain_date", "eval_reports", ["domain", "report_date"], unique=False
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False, comment="daily_brief / system"),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=255), nullable=True),
        sa.Column("read", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=3),
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_notif_user_read", "notifications", ["user_id", "read"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_notif_user_read", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("idx_eval_report_domain_date", table_name="eval_reports")
    op.drop_table("eval_reports")
    op.drop_index("idx_daily_brief_date", table_name="daily_briefs")
    op.drop_table("daily_briefs")
