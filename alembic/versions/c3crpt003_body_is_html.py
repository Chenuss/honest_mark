"""ТП ЧЗ: колонка body_is_html в crpt_emails и crpt_unmatched_emails

Отметка «тело письма — HTML» для корректного отображения в ленте переписки.

Revision ID: c3crpt003
Revises: b2crpt002
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa

revision = "c3crpt003"
down_revision = "b2crpt002"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    for table in ("crpt_emails", "crpt_unmatched_emails"):
        if not _has_column(table, "body_is_html"):
            op.add_column(
                table,
                sa.Column("body_is_html", sa.Boolean(), nullable=False, server_default=sa.false()),
            )


def downgrade() -> None:
    for table in ("crpt_emails", "crpt_unmatched_emails"):
        if _has_column(table, "body_is_html"):
            op.drop_column(table, "body_is_html")
