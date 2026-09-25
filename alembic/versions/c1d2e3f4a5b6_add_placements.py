"""add placement accounts and entries

Revision ID: c1d2e3f4a5b6
Revises: bb558ede14c4
Create Date: 2026-09-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "bb558ede14c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "placement_accounts",
        sa.Column("uuid", sa.TEXT(), nullable=False),
        sa.Column("user_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("name_enc", sa.TEXT(), nullable=False),
        sa.Column("institution_name_enc", sa.TEXT(), nullable=True),
        sa.Column("placement_type_enc", sa.TEXT(), nullable=False),
        sa.Column("expected_return_rate_enc", sa.TEXT(), nullable=True),
        sa.Column("opened_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index(
        op.f("ix_placement_accounts_user_uuid_bidx"),
        "placement_accounts",
        ["user_uuid_bidx"],
        unique=False,
    )
    op.create_table(
        "placement_entries",
        sa.Column("uuid", sa.TEXT(), nullable=False),
        sa.Column("account_uuid", sa.TEXT(), nullable=False),
        sa.Column("type_enc", sa.TEXT(), nullable=False),
        sa.Column("amount_enc", sa.TEXT(), nullable=False),
        sa.Column("occurred_at_enc", sa.TEXT(), nullable=False),
        sa.Column("note_enc", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_uuid"], ["placement_accounts.uuid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index(
        op.f("ix_placement_entries_account_uuid"),
        "placement_entries",
        ["account_uuid"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_placement_entries_account_uuid"), table_name="placement_entries")
    op.drop_table("placement_entries")
    op.drop_index(op.f("ix_placement_accounts_user_uuid_bidx"), table_name="placement_accounts")
    op.drop_table("placement_accounts")
