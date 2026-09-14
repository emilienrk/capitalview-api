"""add last_sync_attempt_at and last_sync_error_enc to bank_account_links

Both NULL on existing links. No attempt was recorded before this migration, so
every link is due exactly as it was; the first sync after deploy stamps it.

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-09-14
"""
from alembic import op
import sqlalchemy as sa

revision = "z7a8b9c0d1e2"
down_revision = "y6z7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_account_links", sa.Column("last_sync_attempt_at", sa.Date(), nullable=True))
    op.add_column("bank_account_links", sa.Column("last_sync_error_enc", sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_account_links", "last_sync_error_enc")
    op.drop_column("bank_account_links", "last_sync_attempt_at")
