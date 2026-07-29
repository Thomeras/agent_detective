"""Loop identity, so the loop check can count rounds instead of cycle size.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-29

``agent_runs.attempt`` / ``.attempt_of`` carry the ``agent_detective.attempt``
and ``agent_detective.attempt_of`` opener-span attributes: which pass a run was,
and of which agent.

Retry attempts have to carry DISTINCT agent names (``builder#1``, ``builder#2``)
or reconstruction draws no edge between them — which also means the graph can no
longer tell one agent that ran eight times from eight agents. Without that, the
loop-anomaly check had only the condensed SCC's member count to read, and member
count is not rounds: a bounded 2x3 nested loop condenses to 21 members and was
reported as 21 runaway iterations at 100% confidence, naming every node in the
graph as origin.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("attempt", sa.Integer, nullable=True))
    op.add_column("agent_runs", sa.Column("attempt_of", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "attempt_of")
    op.drop_column("agent_runs", "attempt")
