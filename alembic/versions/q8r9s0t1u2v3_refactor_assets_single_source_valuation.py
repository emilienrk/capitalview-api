"""refactor assets valuation source of truth

Revision ID: q8r9s0t1u2v3
Revises: p7q8r9s0t1u2
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = "q8r9s0t1u2v3"
down_revision = "p7q8r9s0t1u2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "asset_valuations",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "asset_valuations",
        sa.Column("source", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_asset_valuations_asset_uuid_valued_at_enc",
        "asset_valuations",
        ["asset_uuid", "valued_at_enc"],
        unique=False,
    )
    op.create_index(
        "ix_asset_valuations_asset_uuid_created_at",
        "asset_valuations",
        ["asset_uuid", "created_at"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_asset_valuations_asset_uuid_assets",
        "asset_valuations",
        "assets",
        ["asset_uuid"],
        ["uuid"],
        ondelete="CASCADE",
    )

    # Backfill one initial valuation per asset that has no valuation yet.
    # valued_at_enc keeps encrypted acquisition_date when available, otherwise
    # falls back to plaintext created_at date for legacy rows.
    op.execute(
        sa.text(
            """
            INSERT INTO asset_valuations (
                uuid,
                asset_uuid,
                estimated_value_enc,
                note_enc,
                valued_at_enc,
                source,
                created_at,
                updated_at
            )
            SELECT
                a.uuid || '-init',
                a.uuid,
                a.estimated_value_enc,
                NULL,
                COALESCE(a.acquisition_date_enc, to_char(a.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')),
                'auto_migration',
                a.created_at,
                COALESCE(a.updated_at, a.created_at)
            FROM assets a
            WHERE a.estimated_value_enc IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM asset_valuations v
                WHERE v.asset_uuid = a.uuid
              )
            """
        )
    )

    op.drop_column("assets", "estimated_value_enc")


def downgrade() -> None:
    op.add_column("assets", sa.Column("estimated_value_enc", sa.Text(), nullable=True))

    op.drop_constraint(
        "fk_asset_valuations_asset_uuid_assets",
        "asset_valuations",
        type_="foreignkey",
    )
    op.drop_index("ix_asset_valuations_asset_uuid_created_at", table_name="asset_valuations")
    op.drop_index("ix_asset_valuations_asset_uuid_valued_at_enc", table_name="asset_valuations")

    op.drop_column("asset_valuations", "source")
    op.drop_column("asset_valuations", "updated_at")
