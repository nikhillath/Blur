"""widen redaction_actions.action so FALSE_POSITIVE fits

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("redaction_actions", "action", existing_type=sa.String(8), type_=sa.String(16), existing_nullable=False)


def downgrade() -> None:
    op.execute("UPDATE redaction_actions SET action = 'KEEP' WHERE action = 'FALSE_POSITIVE'")
    op.alter_column("redaction_actions", "action", existing_type=sa.String(16), type_=sa.String(8), existing_nullable=False)
