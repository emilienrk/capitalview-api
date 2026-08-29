"""add bank_authorizations, bank_sessions, bank_account_links

Revision ID: 8a1c6e4f2b90
Revises: 3f5cf85bb741
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8a1c6e4f2b90"
down_revision: Union[str, None] = "3f5cf85bb741"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bank_authorizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("state_bidx", sa.TEXT(), nullable=False),
        sa.Column("aspsp_name_enc", sa.TEXT(), nullable=True),
        sa.Column("aspsp_country_enc", sa.TEXT(), nullable=True),
        sa.Column("authorization_id_enc", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_bank_authorizations_user_uuid_bidx"),
        "bank_authorizations",
        ["user_uuid_bidx"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bank_authorizations_state_bidx"),
        "bank_authorizations",
        ["state_bidx"],
        unique=True,
    )

    op.create_table(
        "bank_sessions",
        sa.Column("uuid", sa.TEXT(), nullable=False),
        sa.Column("user_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("session_id_enc", sa.TEXT(), nullable=False),
        sa.Column("aspsp_name_enc", sa.TEXT(), nullable=True),
        sa.Column("aspsp_country_enc", sa.TEXT(), nullable=True),
        sa.Column("status", sa.TEXT(), nullable=False),
        sa.Column("consent_valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index(
        op.f("ix_bank_sessions_user_uuid_bidx"),
        "bank_sessions",
        ["user_uuid_bidx"],
        unique=False,
    )

    op.create_table(
        "bank_account_links",
        sa.Column("uuid", sa.TEXT(), nullable=False),
        sa.Column("user_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("bank_account_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("session_uuid", sa.TEXT(), nullable=False),
        sa.Column("identification_hash_bidx", sa.TEXT(), nullable=False),
        sa.Column("account_uid_enc", sa.TEXT(), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("anchor_balance_enc", sa.TEXT(), nullable=False),
        sa.Column("last_synced_at", sa.Date(), nullable=False),
        sa.Column("last_reconciliation_gap_enc", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_uuid"], ["bank_sessions.uuid"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index(
        op.f("ix_bank_account_links_user_uuid_bidx"),
        "bank_account_links",
        ["user_uuid_bidx"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bank_account_links_bank_account_uuid_bidx"),
        "bank_account_links",
        ["bank_account_uuid_bidx"],
        unique=True,
    )
    op.create_index(
        op.f("ix_bank_account_links_session_uuid"),
        "bank_account_links",
        ["session_uuid"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bank_account_links_identification_hash_bidx"),
        "bank_account_links",
        ["identification_hash_bidx"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_bank_account_links_identification_hash_bidx"), table_name="bank_account_links")
    op.drop_index(op.f("ix_bank_account_links_session_uuid"), table_name="bank_account_links")
    op.drop_index(op.f("ix_bank_account_links_bank_account_uuid_bidx"), table_name="bank_account_links")
    op.drop_index(op.f("ix_bank_account_links_user_uuid_bidx"), table_name="bank_account_links")
    op.drop_table("bank_account_links")

    op.drop_index(op.f("ix_bank_sessions_user_uuid_bidx"), table_name="bank_sessions")
    op.drop_table("bank_sessions")

    op.drop_index(op.f("ix_bank_authorizations_state_bidx"), table_name="bank_authorizations")
    op.drop_index(op.f("ix_bank_authorizations_user_uuid_bidx"), table_name="bank_authorizations")
    op.drop_table("bank_authorizations")
