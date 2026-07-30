"""Declared node kind, so a node that runs code is not judged as a writer.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-30

``agent_runs.node_kind`` carries the ``agent_detective.node_kind`` opener-span
attribute: how the node actually works, said by the only party that knows — the
caller.

Role was inferred purely from the agent NAME (``PLANNER_PREFIXES`` and friends),
so a ``plan_node`` that makes zero model calls got a planner rubric and was
scored on the phrasing of a plan it never wrote. A name cannot carry this and
never could; there was no way to state it anywhere in the trace.

Free text on purpose: ``deterministic`` and ``tool`` are what the scoring side
acts on, but a value from a newer SDK has to survive the round trip instead of
being rejected by a CHECK constraint the database cannot keep up to date.

NULL for every pre-existing row and for any trace that never declared one:
undeclared is an unmeasured state, not a measured "llm".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("node_kind", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "node_kind")
