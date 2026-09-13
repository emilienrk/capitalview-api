"""add last_balance_type to bank_account_links

NULL on existing links, and left that way: the column records which balance
type the last sync actually read, and no sync before this migration recorded
it. The next sync fills it in — guessing CLBD here would label an account as
resting on an accounting balance without ever having read one.

Revision ID: x5y6z7a8b9c0
Revises: w4x5y6z7a8b9
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "x5y6z7a8b9c0"
down_revision = "w4x5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bank_account_links",
        sa.Column("last_balance_type", sa.TEXT(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("bank_account_links", "last_balance_type")
