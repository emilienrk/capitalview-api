"""add paid_in_outside_banks to placement_accounts

Employee savings fed by the employer or the payslip: its deposits never cross
a bank account, so the bank reading stops looking for them. Every placement
starts off: the user says which ones.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-31
"""
from alembic import op
import sqlalchemy as sa

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "placement_accounts",
        sa.Column("paid_in_outside_banks", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("placement_accounts", "paid_in_outside_banks")
