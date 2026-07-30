"""SQLAlchemy Core tables mirroring the db/alembic migrations.

The Alembic migrations own the schema (0001 base, 0006 run versioning meta,
0009 governance tables + judge_prompt_hash/tool_schema_hash columns); these
definitions are read/update handles only. Keep column names in sync with the
migrations.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()

execution_graphs = sa.Table(
    "execution_graphs",
    metadata,
    sa.Column("graph_id", sa.Uuid(), primary_key=True),
    sa.Column("name", sa.Text()),
    sa.Column("graph_type", sa.Text()),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("ended_at", sa.DateTime(timezone=True)),
    sa.Column("finalized_at", sa.DateTime(timezone=True)),
    sa.Column("total_cost_usd", sa.Numeric()),
    sa.Column("run_count", sa.Integer()),
    sa.Column("late_spans_count", sa.Integer()),
    sa.Column("late_spans_last_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

agent_runs = sa.Table(
    "agent_runs",
    metadata,
    sa.Column("run_id", sa.Uuid(), primary_key=True),
    sa.Column("graph_id", sa.Uuid(), nullable=False),
    sa.Column("agent_name", sa.Text()),
    sa.Column("agent_version", sa.Text()),
    sa.Column("model_name", sa.Text()),
    sa.Column("prompt_hash", sa.Text()),
    sa.Column("tool_schema_hash", sa.Text()),
    sa.Column("parent_run_id", sa.Uuid()),
    sa.Column("trace_id", sa.Text()),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("input_inline", sa.Text()),
    sa.Column("input_overflow_ref", sa.Text()),
    sa.Column("input_bytes", sa.Integer()),
    sa.Column("output_inline", sa.Text()),
    sa.Column("output_overflow_ref", sa.Text()),
    sa.Column("output_bytes", sa.Integer()),
    sa.Column("input_summary", sa.Text()),
    sa.Column("output_summary", sa.Text()),
    sa.Column("quality_score", sa.REAL()),
    sa.Column("score_components", postgresql.JSONB()),
    # What produced the number (0014) and what the trace declared the node to
    # be (0015) — a score has to be able to name its instrument.
    sa.Column("score_weights", postgresql.JSONB()),
    sa.Column("judge_model", sa.Text()),
    sa.Column("node_kind", sa.Text()),
    sa.Column("unscored_reason", sa.Text()),
    sa.Column("input_flawed", sa.Boolean()),
    sa.Column("cost_usd", sa.Numeric()),
    sa.Column("tokens_in", sa.Integer()),
    sa.Column("tokens_out", sa.Integer()),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("ended_at", sa.DateTime(timezone=True)),
)

output_contracts = sa.Table(
    "output_contracts",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("agent_name", sa.Text()),
    sa.Column("agent_version_pattern", sa.Text()),
    sa.Column("json_schema", postgresql.JSONB()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

edges = sa.Table(
    "edges",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("graph_id", sa.Uuid(), nullable=False),
    sa.Column("from_run_id", sa.Uuid()),
    sa.Column("to_run_id", sa.Uuid()),
    sa.Column("type", sa.Text(), nullable=False),
    sa.Column("detection_method", sa.Text()),
)

tier1_verdicts = sa.Table(
    "tier1_verdicts",
    metadata,
    sa.Column("graph_id", sa.Uuid(), primary_key=True),
    sa.Column("terminal_judge_verdict", sa.Text()),
    sa.Column("terminal_judge_score", sa.REAL()),
    sa.Column("terminal_judge_reasoning", sa.Text()),
    sa.Column("flags", postgresql.JSONB()),
    sa.Column("flagged", sa.Boolean(), nullable=False),
    sa.Column("sampled", sa.Boolean(), nullable=False),
    sa.Column("judge_prompt_hash", sa.Text()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

incidents = sa.Table(
    "incidents",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("graph_id", sa.Uuid(), nullable=False),
    sa.Column("incident_key", sa.Text(), nullable=False),
    sa.Column("trigger", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

blame_reports = sa.Table(
    "blame_reports",
    metadata,
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("incident_id", sa.BigInteger(), nullable=False),
    sa.Column("graph_id", sa.Uuid()),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("is_latest", sa.Boolean(), nullable=False),
    sa.Column("report_type", sa.Text()),
    sa.Column("culprit_run_ids", postgresql.ARRAY(sa.Uuid())),
    sa.Column("propagation_path", postgresql.ARRAY(sa.Uuid())),
    sa.Column("confidence", sa.REAL()),
    sa.Column("downstream_cost_usd", sa.Numeric()),
    # Coverage behind the cost (0014): a total over 6 of 28 priced runs is a
    # lower bound, so it travels with the number it qualifies.
    sa.Column("cost_coverage", postgresql.JSONB()),
    sa.Column("unscored_run_ids", postgresql.ARRAY(sa.Uuid())),
    sa.Column("evidence", postgresql.JSONB()),
    sa.Column("judge_prompt_hash", sa.Text()),
    sa.Column("judge_model", sa.Text()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

# --- Migration 0009 governance tables (read-side mirrors) ---

policy_decisions = sa.Table(
    "policy_decisions",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("graph_id", sa.Uuid(), nullable=False),
    sa.Column("rule_name", sa.Text(), nullable=False),
    sa.Column("decision", sa.Text(), nullable=False),  # 'would_block' | 'would_warn' — shadow observations, never enforcement
    sa.Column("detail", sa.Text()),
    sa.Column("mode", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

breaker_state = sa.Table(
    "breaker_state",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("scope_kind", sa.Text(), nullable=False),  # 'agent_name' | 'agent_version'
    sa.Column("scope_value", sa.Text(), nullable=False),
    sa.Column("state", sa.Text(), nullable=False),  # 'open' | 'closed' — a recorded decision; enforcement only if the integration polls it
    sa.Column("reason", sa.Text()),
    sa.Column("opened_at", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

evidence_ledger = sa.Table(
    "evidence_ledger",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("report_id", sa.Integer(), nullable=False),
    sa.Column("evidence_sha256", sa.Text(), nullable=False),
    sa.Column("prev_hash", sa.Text()),
    sa.Column("chain_hash", sa.Text(), nullable=False),
    sa.Column("hmac_sig", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

ground_truth_labels = sa.Table(
    "ground_truth_labels",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("graph_id", sa.Uuid(), nullable=False),
    sa.Column("label", sa.Text(), nullable=False),  # 'ok' | 'bad'
    sa.Column("culprit_run_id", sa.Uuid()),
    sa.Column("source", sa.Text(), nullable=False),
    sa.Column("note", sa.Text()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
