"""ТП ЧЗ: таблицы crpt_support_cases и crpt_emails

Revision ID: a1crpt001
Revises:
Create Date: 2026-10-19

Создаёт таблицы раздела «Работа с ТП Честного Знака»:
  • crpt_support_cases — кейсы обращений в техподдержку ЦРПТ;
  • crpt_emails        — лента писем переписки (timeline).

Существующие таблицы (tickets, users, production_sites, line_equipment)
не изменяются — добавляются только новые таблицы и FK на tickets.id.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1crpt001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CRPT_STATUS_ENUM = sa.Enum(
    "NEW", "WAITING_NUMBER", "IN_PROGRESS", "WAITING_CLARIFICATION",
    "RESOLVED", "CLOSED", "OVERDUE",
    name="crptcasestatus",
)


def upgrade() -> None:
    op.create_table(
        "crpt_support_cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sr_number", sa.String(length=50), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("enterprise_name", sa.String(length=300), nullable=False,
                  server_default='ООО "Белоречье"'),
        sa.Column("inn", sa.String(length=20), nullable=False, server_default="2330012345"),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("status", CRPT_STATUS_ENUM, nullable=False, server_default="NEW"),
        sa.Column("gtin", sa.String(length=50), nullable=True),
        sa.Column("error_code", sa.String(length=50), nullable=True),
        sa.Column("doc_id", sa.String(length=100), nullable=True),
        sa.Column("api_error_object", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("first_response_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_48h", sa.DateTime(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.Column("auto_close_deadline_3d", sa.DateTime(), nullable=True),
        sa.Column("remaining_work_hours", sa.Float(), nullable=True),
        sa.Column("attachments_json", sa.Text(), nullable=True),
        sa.Column("email_thread_id", sa.String(length=255), nullable=True),
        sa.Column("last_crpt_email_body", sa.Text(), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("related_internal_ticket_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["related_internal_ticket_id"], ["tickets.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crpt_cases_sr", "crpt_support_cases", ["sr_number"], unique=True)
    op.create_index("ix_crpt_cases_status", "crpt_support_cases", ["status"])
    op.create_index("ix_crpt_cases_gtin", "crpt_support_cases", ["gtin"])
    op.create_index("ix_crpt_cases_thread", "crpt_support_cases", ["email_thread_id"])
    op.create_index("ix_crpt_cases_rel_ticket", "crpt_support_cases", ["related_internal_ticket_id"])
    op.create_index("ix_crpt_cases_created_by", "crpt_support_cases", ["created_by_id"])

    op.create_table(
        "crpt_emails",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False, server_default="in"),
        sa.Column("message_id", sa.String(length=500), nullable=True),
        sa.Column("from_addr", sa.String(length=300), nullable=True),
        sa.Column("to_addr", sa.String(length=300), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("parsed_sr_number", sa.String(length=50), nullable=True),
        sa.Column("parsed_category", sa.String(length=200), nullable=True),
        sa.Column("parsed_priority", sa.String(length=200), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=True),
        sa.Column("received_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["crpt_support_cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index("ix_crpt_emails_case", "crpt_emails", ["case_id"])
    op.create_index("ix_crpt_emails_received", "crpt_emails", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_crpt_emails_received", table_name="crpt_emails")
    op.drop_index("ix_crpt_emails_case", table_name="crpt_emails")
    op.drop_table("crpt_emails")
    op.drop_index("ix_crpt_cases_created_by", table_name="crpt_support_cases")
    op.drop_index("ix_crpt_cases_rel_ticket", table_name="crpt_support_cases")
    op.drop_index("ix_crpt_cases_thread", table_name="crpt_support_cases")
    op.drop_index("ix_crpt_cases_gtin", table_name="crpt_support_cases")
    op.drop_index("ix_crpt_cases_status", table_name="crpt_support_cases")
    op.drop_index("ix_crpt_cases_sr", table_name="crpt_support_cases")
    op.drop_table("crpt_support_cases")
    # MySQL удаляет enum-тип вместе с таблицей; для других диалектов:
    CRPT_STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
