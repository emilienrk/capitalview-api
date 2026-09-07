"""add history_seeded to bank_account_links

Existing links start at false on purpose: the flag says "the long history fetch
has brought something back", and for every link created before it nobody can
tell. Answering false makes the next sync ask for the history once — which is
exactly the repair the accounts stuck with a flat curve need.

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-09-07
"""
from alembic import op
import sqlalchemy as sa

revision = "w4x5y6z7a8b9"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bank_account_links",
        sa.Column("history_seeded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("bank_account_links", "history_seeded")
