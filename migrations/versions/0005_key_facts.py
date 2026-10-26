"""Key facts: short bullet lines under an LLM story's summary (JSON list of strings).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("story_revisions") as batch:
        batch.add_column(sa.Column("key_facts", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("story_revisions") as batch:
        batch.drop_column("key_facts")
