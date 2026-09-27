"""lowercase user emails

Login now lowercases the email it looks up, so stored emails must match.

Revision ID: d5e9f3a2b7c4
Revises: c4d8e2f1a6b3
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa

revision = "d5e9f3a2b7c4"
down_revision = "c4d8e2f1a6b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    clashes = conn.execute(sa.text(
        "SELECT lower(trim(email)) FROM users GROUP BY lower(trim(email)) HAVING count(*) > 1"
    )).scalars().all()
    if clashes:
        raise RuntimeError(
            f"Accounts differ only by email case, merge or rename them first: {', '.join(clashes)}"
        )
    conn.execute(sa.text("UPDATE users SET email = lower(trim(email)) WHERE email <> lower(trim(email))"))


def downgrade() -> None:
    # The original casing is not kept anywhere.
    pass
