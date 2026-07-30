"""Late-span evidence on graphs, so a finalized graph admits it grew.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-30

Spans can arrive after a graph was finalized (batch span processors flush on
their own timers). The upsert deliberately leaves status/finalized_at alone,
so the graph silently absorbed the proof of its own incompleteness: nothing
re-mapped, nothing re-announced, no trace anywhere.

``late_spans_count`` / ``late_spans_last_at`` are the permanent record: how
many runs landed on the graph after finalization and when the last batch did.
Both stay NULL for graphs that never saw a late span — an unmeasured state,
not a measured zero.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("execution_graphs", sa.Column("late_spans_count", sa.Integer, nullable=True))
    op.add_column(
        "execution_graphs",
        sa.Column("late_spans_last_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_graphs", "late_spans_last_at")
    op.drop_column("execution_graphs", "late_spans_count")
