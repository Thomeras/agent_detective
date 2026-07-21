"""Initial schema (build spec section 5).

Revision ID: 0001
Revises:
Create Date: 2026-07-21

Creates the full Postgres schema: execution_graphs, agent_runs, edges,
tier1_verdicts, tier2_jobs, incidents, blame_reports, agent_stats,
output_contracts, checkpoints.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_graphs",
        sa.Column("graph_id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("graph_type", sa.Text()),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint("status IN ('active','finalized')", name="ck_execution_graphs_status"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("total_cost_usd", sa.Numeric()),
        sa.Column("run_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_execution_graphs_type_started", "execution_graphs", ["graph_type", "started_at"])
    op.create_index("ix_execution_graphs_status", "execution_graphs", ["status"])

    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("graph_id", sa.Uuid(), sa.ForeignKey("execution_graphs.graph_id"), nullable=False),
        sa.Column("agent_name", sa.Text()),
        sa.Column("agent_version", sa.Text()),
        sa.Column("parent_run_id", sa.Uuid()),
        sa.Column("trace_id", sa.Text()),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint("status IN ('ok','degraded','failed')", name="ck_agent_runs_status"),
            nullable=False,
        ),
        sa.Column("input_inline", sa.Text()),
        sa.Column("input_overflow_ref", sa.Text()),
        sa.Column("input_bytes", sa.Integer()),
        sa.Column("output_inline", sa.Text()),
        sa.Column("output_overflow_ref", sa.Text()),
        sa.Column("output_bytes", sa.Integer()),
        # Derived from the payloads; UI display only.
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
    op.create_index("ix_agent_runs_graph_id", "agent_runs", ["graph_id"])
    op.create_index("ix_agent_runs_agent_started", "agent_runs", ["agent_name", "started_at"])
    op.create_index("ix_agent_runs_trace_id", "agent_runs", ["trace_id"])

    op.create_table(
        "edges",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("graph_id", sa.Uuid(), sa.ForeignKey("execution_graphs.graph_id"), nullable=False),
        sa.Column("from_run_id", sa.Uuid()),
        sa.Column("to_run_id", sa.Uuid()),
        sa.Column(
            "type",
            sa.Text(),
            sa.CheckConstraint("type IN ('SPAWN','A2A_MESSAGE','TOOL_DELEGATION')", name="ck_edges_type"),
            nullable=False,
        ),
        sa.Column("detection_method", sa.Text()),
        sa.UniqueConstraint("graph_id", "from_run_id", "to_run_id", "type", name="uq_edges_edge"),
    )
    op.create_index("ix_edges_graph_id", "edges", ["graph_id"])

    op.create_table(
        "tier1_verdicts",
        sa.Column("graph_id", sa.Uuid(), sa.ForeignKey("execution_graphs.graph_id"), primary_key=True),
        sa.Column(
            "terminal_judge_verdict",
            sa.Text(),
            sa.CheckConstraint("terminal_judge_verdict IN ('ok','bad','error')", name="ck_tier1_verdicts_verdict"),
        ),
        sa.Column("terminal_judge_score", sa.REAL()),
        sa.Column("terminal_judge_reasoning", sa.Text()),
        # e.g. ["failed_runs","cost_overrun","loop_anomaly","schema_violation","degenerate_output"]
        sa.Column("flags", postgresql.JSONB()),
        sa.Column("flagged", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sampled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "tier2_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("graph_id", sa.Uuid(), sa.ForeignKey("execution_graphs.graph_id"), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "trigger",
            sa.Text(),
            sa.CheckConstraint("trigger IN ('tier1','manual','sampled')", name="ck_tier2_jobs_trigger"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint("status IN ('queued','running','done','failed')", name="ck_tier2_jobs_status"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_tier2_jobs_status", "tier2_jobs", ["status"])

    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("graph_id", sa.Uuid(), sa.ForeignKey("execution_graphs.graph_id"), nullable=False),
        sa.Column("incident_key", sa.Text(), nullable=False),
        sa.Column(
            "trigger",
            sa.Text(),
            sa.CheckConstraint(
                "trigger IN ('terminal_failure','degraded_quality','cost_overrun','loop_detected','manual')",
                name="ck_incidents_trigger",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint("status IN ('open','acknowledged','resolved')", name="ck_incidents_status"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("graph_id", "incident_key", name="uq_incidents_graph_key"),
    )

    op.create_table(
        "blame_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("incident_id", sa.BigInteger(), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("graph_id", sa.Uuid()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_latest", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "report_type",
            sa.Text(),
            sa.CheckConstraint(
                "report_type IN ('cut_point','multi_culprit','composition_failure',"
                "'loop_detected','root_cause_external','unclassified')",
                name="ck_blame_reports_type",
            ),
        ),
        sa.Column("culprit_run_ids", postgresql.ARRAY(sa.Uuid())),
        sa.Column("propagation_path", postgresql.ARRAY(sa.Uuid())),
        sa.Column("confidence", sa.REAL()),
        sa.Column("downstream_cost_usd", sa.Numeric()),
        sa.Column("unscored_run_ids", postgresql.ARRAY(sa.Uuid())),
        sa.Column("evidence", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("incident_id", "version", name="uq_blame_reports_incident_version"),
    )
    op.create_index(
        "ix_blame_reports_latest",
        "blame_reports",
        ["incident_id"],
        postgresql_where=sa.text("is_latest"),
    )

    op.create_table(
        "agent_stats",
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("graph_type", sa.Text(), nullable=False),
        sa.Column("tokens_out_mean", sa.REAL()),
        sa.Column("tokens_out_std", sa.REAL()),
        sa.Column("iterations_mean", sa.REAL()),
        sa.Column("iterations_std", sa.REAL()),
        sa.Column("sample_count", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("agent_name", "graph_type", name="pk_agent_stats"),
    )

    op.create_table(
        "output_contracts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("agent_name", sa.Text()),
        sa.Column("agent_version_pattern", sa.Text()),
        sa.Column("json_schema", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "checkpoints",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.run_id"), primary_key=True),
        sa.Column("input_ref", sa.Text()),
        sa.Column("model_config", postgresql.JSONB()),
        sa.Column("tool_calls", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("checkpoints")
    op.drop_table("output_contracts")
    op.drop_table("agent_stats")
    op.drop_index("ix_blame_reports_latest", table_name="blame_reports")
    op.drop_table("blame_reports")
    op.drop_table("incidents")
    op.drop_index("ix_tier2_jobs_status", table_name="tier2_jobs")
    op.drop_table("tier2_jobs")
    op.drop_table("tier1_verdicts")
    op.drop_index("ix_edges_graph_id", table_name="edges")
    op.drop_table("edges")
    op.drop_index("ix_agent_runs_trace_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_started", table_name="agent_runs")
    op.drop_index("ix_agent_runs_graph_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_execution_graphs_status", table_name="execution_graphs")
    op.drop_index("ix_execution_graphs_type_started", table_name="execution_graphs")
    op.drop_table("execution_graphs")
