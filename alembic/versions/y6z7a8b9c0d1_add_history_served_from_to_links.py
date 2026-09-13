"""add history_served_from_enc to bank_account_links

NULL on existing links, and left that way: the column records how far back a
seeding pass reached, and those passes ran before anything recorded it.
Backfilling from the stored operations would report what the database holds,
not what the bank serves — an export import widens the first and says nothing
about the second.

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "y6z7a8b9c0d1"
down_revision = "x5y6z7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bank_account_links",
        sa.Column("history_served_from_enc", sa.TEXT(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bank_account_links", "history_served_from_enc")
