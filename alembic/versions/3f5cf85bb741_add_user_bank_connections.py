"""add user_bank_connections

Revision ID: 3f5cf85bb741
Revises: 97e6b1147cb5
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3f5cf85bb741"
down_revision: Union[str, None] = "97e6b1147cb5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_bank_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_uuid_bidx", sa.TEXT(), nullable=False),
        sa.Column("application_id_enc", sa.TEXT(), nullable=True),
        sa.Column("private_key_enc", sa.TEXT(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_bank_connections_user_uuid_bidx"),
        "user_bank_connections",
        ["user_uuid_bidx"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_bank_connections_user_uuid_bidx"), table_name="user_bank_connections")
    op.drop_table("user_bank_connections")
