"""add_account_history_table

Revision ID: m4n5o6p7q8r9
Revises: l3m4n5o6p7q8
Create Date: 2026-03-12 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m4n5o6p7q8r9"
down_revision: Union[str, None] = "l3m4n5o6p7q8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_history",
        sa.Column("uuid", sa.Text, primary_key=True, nullable=False),
        sa.Column("user_uuid_bidx", sa.Text, nullable=False),
        sa.Column("account_id_bidx", sa.Text, nullable=False),
        sa.Column("account_type", sa.Text, nullable=False),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("total_value_enc", sa.Text, nullable=False),
        sa.Column("total_invested_enc", sa.Text, nullable=False),
        sa.Column("daily_pnl_enc", sa.Text, nullable=True),
        sa.Column("positions_enc", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id_bidx",
            "snapshot_date",
            name="uq_account_history_account_date",
        ),
    )

    op.create_index("ix_account_history_user_uuid_bidx", "account_history", ["user_uuid_bidx"])
    op.create_index("ix_account_history_account_id_bidx", "account_history", ["account_id_bidx"])
    op.create_index("ix_account_history_account_type", "account_history", ["account_type"])
    op.create_index(
        "ix_account_history_account_date",
        "account_history",
        ["account_id_bidx", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_history_account_date", table_name="account_history")
    op.drop_index("ix_account_history_account_type", table_name="account_history")
    op.drop_index("ix_account_history_account_id_bidx", table_name="account_history")
    op.drop_index("ix_account_history_user_uuid_bidx", table_name="account_history")
    op.drop_table("account_history")
