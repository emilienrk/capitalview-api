"""add selected_vision_model to user_ai_providers

The photo import gets its own model choice beside the chat's, so a provider
can read images with one model and chat with another. Each row starts with the
model it already called for the photo import: its chosen model, except on
DeepSeek, whose models never served it until now and stay on "automatic".

Revision ID: b9c0d1e2f3a4
Revises: 7968262b9b90
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "b9c0d1e2f3a4"
down_revision = "7968262b9b90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_ai_providers", sa.Column("selected_vision_model", sa.String(), nullable=True))
    op.execute(
        "UPDATE user_ai_providers SET selected_vision_model = selected_model "
        "WHERE provider <> 'deepseek'"
    )


def downgrade() -> None:
    op.drop_column("user_ai_providers", "selected_vision_model")
