"""add balance_checkpoints_enc to bank_account_links

NULL on existing links: the reconciliation check falls back to the link's own
anchor until the first syncs have recorded a few readings.

Revision ID: a8b9c0d1e2f3
Revises: z7a8b9c0d1e2
Create Date: 2026-09-15
"""
from alembic import op
import sqlalchemy as sa

revision = "a8b9c0d1e2f3"
down_revision = "z7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_account_links", sa.Column("balance_checkpoints_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_account_links", "balance_checkpoints_enc")
