"""SQLAlchemy Core tables mirroring db/alembic/versions/0001_initial_schema.py.

The Alembic migration owns the schema; these definitions are read/update
handles only. Keep column names in sync with the migration.
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
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

agent_runs = sa.Table(
    "agent_runs",
    metadata,
    sa.Column("run_id", sa.Uuid(), primary_key=True),
    sa.Column("graph_id", sa.Uuid(), nullable=False),
    sa.Column("agent_name", sa.Text()),
    sa.Column("agent_version", sa.Text()),
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
    sa.Column("unscored_reason", sa.Text()),
    sa.Column("input_flawed", sa.Boolean()),
    sa.Column("cost_usd", sa.Numeric()),
    sa.Column("tokens_in", sa.Integer()),
    sa.Column("tokens_out", sa.Integer()),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("ended_at", sa.DateTime(timezone=True)),
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
    sa.Column("unscored_run_ids", postgresql.ARRAY(sa.Uuid())),
    sa.Column("evidence", postgresql.JSONB()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
