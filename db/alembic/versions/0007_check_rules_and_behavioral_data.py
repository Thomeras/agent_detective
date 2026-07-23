"""Check rules table + behavioral data columns for the section-A checks.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-23

Three changes (design: docs/deterministic-signals.md — named deterministic
signals with provenance; this migration lands only the data plumbing, the
checks themselves are wired separately):

1. NEW table ``check_rules``: registered deterministic requirements the
   worker evaluates against runs — required document sections
   (``kind='required_section'``), numeric sum invariants
   (``kind='sum_invariant'``), and tool argument schemas
   (``kind='tool_schema'``). ``agent_name`` / ``graph_type`` are NULL for
   "applies to any"; ``spec`` (JSONB) holds the kind-specific rule payload.
   ``kind`` is CHECK-constrained to the three known values so a typo'd rule
   fails at insert time, not silently at evaluation time.

2. ``agent_runs`` gains a nullable Text column ``tool_calls``: a compact
   JSON digest of the run's TOOL member spans (array of
   ``{"name","args_sha","status"}`` in execution order), derived by
   otel_mapper and landed verbatim by ingest. Deliberately Text, not JSONB:
   the worker parses it tolerantly, so a malformed digest string must not
   fail ingest (same rationale as ``artifact_meta`` in 0006).

3. ``agent_stats`` gains nullable REAL columns ``cost_mean``, ``cost_std``,
   ``tokens_out_m2``, ``cost_m2`` and ``iterations_m2``: cost joins the
   baselined metrics, and the ``*_m2`` columns are Welford running-variance
   accumulators (sum of squared deviations from the running mean) so the
   worker can update mean/std incrementally in O(1) per sample without
   re-reading history. The ``*_std`` columns remain the derived view
   (``sqrt(m2 / (n - 1))`` for n > 1).

Downgrade drops the table and all added columns. The ``*_m2`` accumulators
and ``cost_mean``/``cost_std`` are not recoverable after downgrade.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "check_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_name", sa.Text(), nullable=True),  # NULL = any agent
        sa.Column("graph_type", sa.Text(), nullable=True),  # NULL = any graph type
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('required_section','sum_invariant','tool_schema')",
            name="ck_check_rules_kind",
        ),
    )

    op.add_column("agent_runs", sa.Column("tool_calls", sa.Text(), nullable=True))

    op.add_column("agent_stats", sa.Column("cost_mean", sa.REAL(), nullable=True))
    op.add_column("agent_stats", sa.Column("cost_std", sa.REAL(), nullable=True))
    op.add_column("agent_stats", sa.Column("tokens_out_m2", sa.REAL(), nullable=True))
    op.add_column("agent_stats", sa.Column("cost_m2", sa.REAL(), nullable=True))
    op.add_column("agent_stats", sa.Column("iterations_m2", sa.REAL(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_stats", "iterations_m2")
    op.drop_column("agent_stats", "cost_m2")
    op.drop_column("agent_stats", "tokens_out_m2")
    op.drop_column("agent_stats", "cost_std")
    op.drop_column("agent_stats", "cost_mean")

    op.drop_column("agent_runs", "tool_calls")

    op.drop_table("check_rules")
