"""ТП ЧЗ: таблица crpt_unmatched_emails (непривязанные письма support@crpt.ru)

Revision ID: b2crpt002
Revises: a1crpt001
Create Date: 2026-10-22

Фикс привязки входящих: письма, пришедшие до того как в БД появился SR
(или не нашедшие кейс), сохраняются в crpt_unmatched_emails и
повторно пробуют быть привязанными при каждом проходе парсера.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2crpt002"
down_revision: Union[str, None] = "a1crpt001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crpt_unmatched_emails",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=500), nullable=True),
        sa.Column("from_addr", sa.String(length=300), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("parsed_sr_number", sa.String(length=50), nullable=True),
        sa.Column("received_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("matched_case_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["matched_case_id"], ["crpt_support_cases.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index("ix_crpt_unmatched_sr", "crpt_unmatched_emails", ["parsed_sr_number"])
    op.create_index("ix_crpt_unmatched_received", "crpt_unmatched_emails", ["received_at"])
    op.create_index("ix_crpt_unmatched_case", "crpt_unmatched_emails", ["matched_case_id"])


def downgrade() -> None:
    op.drop_index("ix_crpt_unmatched_case", table_name="crpt_unmatched_emails")
    op.drop_index("ix_crpt_unmatched_received", table_name="crpt_unmatched_emails")
    op.drop_index("ix_crpt_unmatched_sr", table_name="crpt_unmatched_emails")
    op.drop_table("crpt_unmatched_emails")
