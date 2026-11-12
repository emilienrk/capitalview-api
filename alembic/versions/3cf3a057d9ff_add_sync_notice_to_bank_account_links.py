"""add sync notice to bank account links

Revision ID: 3cf3a057d9ff
Revises: 399aad49ce10
Create Date: 2026-11-12 22:32:12.403865

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3cf3a057d9ff'
down_revision: Union[str, None] = '399aad49ce10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bank_account_links', sa.Column('sync_notice_enc', sa.TEXT(), nullable=True))


def downgrade() -> None:
    op.drop_column('bank_account_links', 'sync_notice_enc')
