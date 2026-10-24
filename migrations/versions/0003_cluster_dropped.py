"""Remember the facts a cluster's story was dropped on, so it is not retried unchanged.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("clusters") as batch:
        batch.add_column(sa.Column("dropped_on", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("clusters") as batch:
        batch.drop_column("dropped_on")
