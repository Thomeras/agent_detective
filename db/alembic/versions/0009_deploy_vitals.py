"""Deploy-vital platform plumbing: provenance columns + five new tables.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-23

Data plumbing for docs/roadmap.md section 2 (deploy-vital platform features).
Six changes:

1. ``agent_runs`` gains nullable Text ``tool_schema_hash`` — fingerprint of
   the tool schema the run executed under, completing the per-run identity
   tuple (agent_version, model_name, prompt_hash, tool_schema_hash) used by
   the version-diff and leaderboard views (roadmap 2.1).

2. ``tier1_verdicts`` and ``blame_reports`` gain nullable Text
   ``judge_prompt_hash`` — the worker's own judge-prompt fingerprint
   (12 hex of sha256 over the sorted worker/prompts/*.md bytes), so
   calibration can be sliced by judge-prompt version (roadmap 2.7). NULL on
   rows that predate stamping. Known limitation: the judge MODEL is not
   recorded, only the prompt set.

3. NEW table ``policy_rules``: named predicates over tier1 flags /
   deterministic signals / report types / cost / scores with action
   'warn'|'block'. ``shadow`` defaults true — Agent Detective analyzes after
   the fact, so a rule firing is an annotation ("would have blocked"), never
   an interception (roadmap 2.2).

4. NEW table ``policy_decisions``: one row per rule that fired on a graph.
   ``decision`` is CHECK-constrained to 'would_block'|'would_warn' — the
   names themselves keep the honesty requirement (shadow mode records what
   WOULD have happened; enforcement does not exist here).

5. NEW table ``breaker_state``: recorded circuit-breaker decisions per
   agent_name/agent_version scope. Agent Detective observes and cannot stop
   anything — this table RECORDS an open/closed decision; enforcement only
   happens if the integration polls it (roadmap 2.3).

6. NEW table ``evidence_ledger``: append-only hash chain over
   canonically-serialized blame evidence (sha256 chain + HMAC signature),
   written inside the same transaction that persists the report
   (roadmap 2.6).

7. NEW table ``ground_truth_labels``: human ok/bad verdict feedback per
   graph with an optional culprit pick — the calibration and eval-fixture
   source (roadmap 2.7).

Downgrade drops everything added. Ledger chains, recorded policy decisions,
breaker history and human labels are not recoverable after downgrade.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("tool_schema_hash", sa.Text(), nullable=True))
    op.add_column(
        "tier1_verdicts", sa.Column("judge_prompt_hash", sa.Text(), nullable=True)
    )
    op.add_column(
        "blame_reports", sa.Column("judge_prompt_hash", sa.Text(), nullable=True)
    )

    op.create_table(
        "policy_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("predicate", postgresql.JSONB(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("shadow", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("action IN ('warn','block')", name="ck_policy_rules_action"),
    )

    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("rule_name", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False, server_default="shadow"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('would_block','would_warn')",
            name="ck_policy_decisions_decision",
        ),
    )
    op.create_index("ix_policy_decisions_graph", "policy_decisions", ["graph_id"])

    op.create_table(
        "breaker_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_value", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "scope_kind IN ('agent_name','agent_version')",
            name="ck_breaker_state_scope_kind",
        ),
        sa.CheckConstraint("state IN ('open','closed')", name="ck_breaker_state_state"),
        sa.UniqueConstraint("scope_kind", "scope_value", name="uq_breaker_state_scope"),
    )

    op.create_table(
        "evidence_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.Column("prev_hash", sa.Text(), nullable=True),
        sa.Column("chain_hash", sa.Text(), nullable=False),
        sa.Column("hmac_sig", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_evidence_ledger_report", "evidence_ledger", ["report_id"])

    op.create_table(
        "ground_truth_labels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("graph_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("culprit_run_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="human"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("label IN ('ok','bad')", name="ck_ground_truth_labels_label"),
    )
    op.create_index("ix_ground_truth_graph", "ground_truth_labels", ["graph_id"])


def downgrade() -> None:
    op.drop_index("ix_ground_truth_graph", table_name="ground_truth_labels")
    op.drop_table("ground_truth_labels")

    op.drop_index("ix_evidence_ledger_report", table_name="evidence_ledger")
    op.drop_table("evidence_ledger")

    op.drop_table("breaker_state")

    op.drop_index("ix_policy_decisions_graph", table_name="policy_decisions")
    op.drop_table("policy_decisions")

    op.drop_table("policy_rules")

    op.drop_column("blame_reports", "judge_prompt_hash")
    op.drop_column("tier1_verdicts", "judge_prompt_hash")
    op.drop_column("agent_runs", "tool_schema_hash")
