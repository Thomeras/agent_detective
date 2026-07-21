"""Postgres repository for the worker (build spec sections 4 and 5).

All access sits behind the ``Repo`` protocol (the test seam); ``PgRepo`` is the
async SQLAlchemy Core / asyncpg implementation. Table metadata mirrors
db/alembic/versions/0001_initial_schema.py (only the columns the worker reads
and writes).

Idempotency is built into the writes so redelivery is a no-op (spec sections 4
and 10):

- ``tier1_verdicts`` upsert on the PK ``graph_id``;
- ``tier2_jobs`` claim via ``INSERT ... ON CONFLICT (dedup_key) DO NOTHING``;
- ``incidents`` upsert on ``(graph_id, incident_key)``, reporting whether the
  row was newly inserted via the ``xmax = 0`` trick;
- ``blame_reports`` versioned with ``is_latest`` flipped in one transaction.

The heavy tier2 write (node scores + incident + blame report + job completion)
is one transaction, so callers XACK only after it commits.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import (
    REAL,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .types import (
    AgentStat,
    BlameDraft,
    ClaimResult,
    EdgeRecord,
    GraphBundle,
    NodeScoreRow,
    OutputContract,
    RunRecord,
    Tier1Verdict,
    Tier2Outcome,
    AlertContext,
)

metadata = MetaData()

execution_graphs = Table(
    "execution_graphs",
    metadata,
    Column("graph_id", Uuid, primary_key=True),
    Column("name", Text),
    Column("graph_type", Text),
    Column("status", Text, nullable=False),
    Column("total_cost_usd", Numeric),
    Column("run_count", Integer),
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("run_id", Uuid, primary_key=True),
    Column("graph_id", Uuid, nullable=False),
    Column("agent_name", Text),
    Column("agent_version", Text),
    Column("status", Text, nullable=False),
    Column("input_inline", Text),
    Column("input_overflow_ref", Text),
    Column("output_inline", Text),
    Column("output_overflow_ref", Text),
    Column("output_bytes", Integer),
    Column("quality_score", REAL),
    Column("score_components", JSONB),
    Column("unscored_reason", Text),
    Column("input_flawed", Boolean),
    Column("cost_usd", Numeric),
    Column("tokens_in", Integer),
    Column("tokens_out", Integer),
    Column("started_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
)

edges = Table(
    "edges",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("graph_id", Uuid, nullable=False),
    Column("from_run_id", Uuid),
    Column("to_run_id", Uuid),
    Column("type", Text, nullable=False),
)

tier1_verdicts = Table(
    "tier1_verdicts",
    metadata,
    Column("graph_id", Uuid, primary_key=True),
    Column("terminal_judge_verdict", Text),
    Column("terminal_judge_score", REAL),
    Column("terminal_judge_reasoning", Text),
    Column("flags", JSONB),
    Column("flagged", Boolean, nullable=False),
    Column("sampled", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

tier2_jobs = Table(
    "tier2_jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("graph_id", Uuid, nullable=False),
    Column("dedup_key", Text, nullable=False, unique=True),
    Column("trigger", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)

incidents = Table(
    "incidents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("graph_id", Uuid, nullable=False),
    Column("incident_key", Text, nullable=False),
    Column("trigger", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

blame_reports = Table(
    "blame_reports",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", Integer, nullable=False),
    Column("graph_id", Uuid),
    Column("version", Integer, nullable=False),
    Column("is_latest", Boolean, nullable=False),
    Column("report_type", Text),
    Column("culprit_run_ids", ARRAY(Uuid)),
    Column("propagation_path", ARRAY(Uuid)),
    Column("confidence", REAL),
    Column("downstream_cost_usd", Numeric),
    Column("unscored_run_ids", ARRAY(Uuid)),
    Column("evidence", JSONB),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

agent_stats = Table(
    "agent_stats",
    metadata,
    Column("agent_name", Text, nullable=False),
    Column("graph_type", Text, nullable=False),
    Column("tokens_out_mean", REAL),
    Column("tokens_out_std", REAL),
    Column("iterations_mean", REAL),
    Column("iterations_std", REAL),
    Column("sample_count", Integer),
)

output_contracts = Table(
    "output_contracts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_name", Text),
    Column("agent_version_pattern", Text),
    Column("json_schema", JSONB),
)


class Repo(Protocol):
    """Persistence seam; faked by an in-memory implementation in tests."""

    async def load_graph(self, graph_id: UUID) -> GraphBundle | None: ...

    async def upsert_tier1_verdict(self, verdict: Tier1Verdict) -> None: ...

    async def read_tier1_verdict(self, graph_id: UUID) -> Tier1Verdict | None: ...

    async def claim_tier2_job(
        self, graph_id: UUID, dedup_key: str, trigger: str
    ) -> ClaimResult: ...

    async def fail_tier2_job(self, dedup_key: str, error: str) -> None: ...

    async def read_agent_stats(self, graph_type: str | None) -> dict[str, AgentStat]: ...

    async def read_output_contracts(self) -> list[OutputContract]: ...

    async def persist_tier2_result(
        self,
        *,
        dedup_key: str,
        node_scores: list[NodeScoreRow],
        graph_id: UUID,
        incident_key: str | None,
        incident_trigger: str | None,
        blame: BlameDraft | None,
    ) -> Tier2Outcome: ...

    async def load_alert_context(self, incident_id: int) -> AlertContext | None: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class PgRepo:
    def __init__(self, engine: "object") -> None:
        self._engine = engine

    async def load_graph(self, graph_id: UUID) -> GraphBundle | None:
        graph_stmt = select(
            execution_graphs.c.graph_id,
            execution_graphs.c.name,
            execution_graphs.c.graph_type,
            execution_graphs.c.total_cost_usd,
            execution_graphs.c.run_count,
        ).where(execution_graphs.c.graph_id == graph_id)
        runs_stmt = select(agent_runs).where(agent_runs.c.graph_id == graph_id)
        edges_stmt = select(
            edges.c.from_run_id, edges.c.to_run_id, edges.c.type
        ).where(edges.c.graph_id == graph_id)
        async with self._engine.connect() as conn:
            graph = (await conn.execute(graph_stmt)).first()
            if graph is None:
                return None
            run_rows = (await conn.execute(runs_stmt)).all()
            edge_rows = (await conn.execute(edges_stmt)).all()
        runs = [
            RunRecord(
                run_id=r.run_id,
                graph_id=r.graph_id,
                agent_name=r.agent_name,
                agent_version=r.agent_version,
                status=r.status,
                input_inline=r.input_inline,
                input_overflow_ref=r.input_overflow_ref,
                output_inline=r.output_inline,
                output_overflow_ref=r.output_overflow_ref,
                output_bytes=r.output_bytes,
                cost_usd=float(r.cost_usd) if r.cost_usd is not None else None,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                started_at=r.started_at,
                ended_at=r.ended_at,
            )
            for r in run_rows
        ]
        graph_edges = [
            EdgeRecord(from_run_id=e.from_run_id, to_run_id=e.to_run_id, type=e.type)
            for e in edge_rows
        ]
        return GraphBundle(
            graph_id=graph.graph_id,
            name=graph.name,
            graph_type=graph.graph_type,
            total_cost_usd=float(graph.total_cost_usd)
            if graph.total_cost_usd is not None
            else None,
            run_count=graph.run_count,
            runs=runs,
            edges=graph_edges,
        )

    async def upsert_tier1_verdict(self, verdict: Tier1Verdict) -> None:
        stmt = pg_insert(tier1_verdicts).values(
            graph_id=verdict.graph_id,
            terminal_judge_verdict=verdict.terminal_judge_verdict,
            terminal_judge_score=verdict.terminal_judge_score,
            terminal_judge_reasoning=verdict.terminal_judge_reasoning,
            flags=verdict.flags,
            flagged=verdict.flagged,
            sampled=verdict.sampled,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["graph_id"],
            set_={
                "terminal_judge_verdict": stmt.excluded.terminal_judge_verdict,
                "terminal_judge_score": stmt.excluded.terminal_judge_score,
                "terminal_judge_reasoning": stmt.excluded.terminal_judge_reasoning,
                "flags": stmt.excluded.flags,
                "flagged": stmt.excluded.flagged,
                "sampled": stmt.excluded.sampled,
                "updated_at": func.now(),
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def read_tier1_verdict(self, graph_id: UUID) -> Tier1Verdict | None:
        stmt = select(tier1_verdicts).where(tier1_verdicts.c.graph_id == graph_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        return Tier1Verdict(
            graph_id=row.graph_id,
            terminal_judge_verdict=row.terminal_judge_verdict,
            terminal_judge_score=row.terminal_judge_score,
            terminal_judge_reasoning=row.terminal_judge_reasoning,
            flags=list(row.flags or []),
            flagged=row.flagged,
            sampled=row.sampled,
        )

    async def claim_tier2_job(
        self, graph_id: UUID, dedup_key: str, trigger: str
    ) -> ClaimResult:
        insert_stmt = (
            pg_insert(tier2_jobs)
            .values(
                graph_id=graph_id,
                dedup_key=dedup_key,
                trigger=trigger,
                status="running",
                attempts=1,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(tier2_jobs.c.id)
        )
        async with self._engine.begin() as conn:
            claimed = (await conn.execute(insert_stmt)).first()
            if claimed is not None:
                return ClaimResult(claimed=True, status="running")
            existing = (
                await conn.execute(
                    select(tier2_jobs.c.status).where(tier2_jobs.c.dedup_key == dedup_key)
                )
            ).first()
        return ClaimResult(claimed=False, status=existing.status if existing else None)

    async def fail_tier2_job(self, dedup_key: str, error: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(tier2_jobs)
                .where(tier2_jobs.c.dedup_key == dedup_key)
                .values(status="failed", error=error, finished_at=func.now())
            )

    async def read_agent_stats(self, graph_type: str | None) -> dict[str, AgentStat]:
        stmt = select(agent_stats)
        if graph_type is not None:
            stmt = stmt.where(agent_stats.c.graph_type == graph_type)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {
            row.agent_name: AgentStat(
                tokens_out_mean=row.tokens_out_mean,
                tokens_out_std=row.tokens_out_std,
                iterations_mean=row.iterations_mean,
                iterations_std=row.iterations_std,
                sample_count=row.sample_count,
            )
            for row in rows
        }

    async def read_output_contracts(self) -> list[OutputContract]:
        stmt = select(
            output_contracts.c.agent_name,
            output_contracts.c.agent_version_pattern,
            output_contracts.c.json_schema,
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            OutputContract(
                agent_name=row.agent_name,
                agent_version_pattern=row.agent_version_pattern,
                json_schema=row.json_schema or {},
            )
            for row in rows
        ]

    async def persist_tier2_result(
        self,
        *,
        dedup_key: str,
        node_scores: list[NodeScoreRow],
        graph_id: UUID,
        incident_key: str | None,
        incident_trigger: str | None,
        blame: BlameDraft | None,
    ) -> Tier2Outcome:
        async with self._engine.begin() as conn:
            for row in node_scores:
                await conn.execute(
                    sa.update(agent_runs)
                    .where(agent_runs.c.run_id == row.run_id)
                    .values(
                        quality_score=row.quality_score,
                        score_components=row.score_components,
                        unscored_reason=row.unscored_reason,
                        input_flawed=row.input_flawed,
                    )
                )

            incident_id: int | None = None
            is_new = False
            blame_report_id: int | None = None

            if incident_key is not None and incident_trigger is not None:
                incident_stmt = (
                    pg_insert(incidents)
                    .values(
                        graph_id=graph_id,
                        incident_key=incident_key,
                        trigger=incident_trigger,
                        status="open",
                    )
                    .on_conflict_do_update(
                        index_elements=["graph_id", "incident_key"],
                        set_={"updated_at": func.now()},
                    )
                    .returning(
                        incidents.c.id,
                        sa.literal_column("(xmax = 0)").label("inserted"),
                    )
                )
                incident_row = (await conn.execute(incident_stmt)).first()
                incident_id = incident_row.id
                is_new = bool(incident_row.inserted)

                if blame is not None:
                    await conn.execute(
                        sa.update(blame_reports)
                        .where(blame_reports.c.incident_id == incident_id)
                        .values(is_latest=False)
                    )
                    next_version = (
                        await conn.execute(
                            select(
                                func.coalesce(func.max(blame_reports.c.version), 0) + 1
                            ).where(blame_reports.c.incident_id == incident_id)
                        )
                    ).scalar_one()
                    blame_report_id = (
                        await conn.execute(
                            pg_insert(blame_reports)
                            .values(
                                incident_id=incident_id,
                                graph_id=graph_id,
                                version=next_version,
                                is_latest=True,
                                report_type=blame.report_type,
                                culprit_run_ids=blame.culprit_run_ids,
                                propagation_path=blame.propagation_path,
                                confidence=blame.confidence,
                                downstream_cost_usd=blame.downstream_cost_usd,
                                unscored_run_ids=blame.unscored_run_ids,
                                evidence=blame.evidence,
                            )
                            .returning(blame_reports.c.id)
                        )
                    ).scalar_one()

            await conn.execute(
                sa.update(tier2_jobs)
                .where(tier2_jobs.c.dedup_key == dedup_key)
                .values(status="done", finished_at=func.now())
            )
        return Tier2Outcome(
            incident_id=incident_id, is_new=is_new, blame_report_id=blame_report_id
        )

    async def load_alert_context(self, incident_id: int) -> AlertContext | None:
        stmt = (
            select(
                incidents.c.id,
                incidents.c.graph_id,
                incidents.c.trigger,
                blame_reports.c.report_type,
                blame_reports.c.culprit_run_ids,
                blame_reports.c.confidence,
                blame_reports.c.downstream_cost_usd,
            )
            .select_from(
                incidents.outerjoin(
                    blame_reports,
                    (blame_reports.c.incident_id == incidents.c.id)
                    & (blame_reports.c.is_latest),
                )
            )
            .where(incidents.c.id == incident_id)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        return AlertContext(
            incident_id=row.id,
            graph_id=row.graph_id,
            trigger=row.trigger,
            report_type=row.report_type,
            culprit_run_ids=list(row.culprit_run_ids or []),
            confidence=row.confidence,
            downstream_cost_usd=float(row.downstream_cost_usd)
            if row.downstream_cost_usd is not None
            else None,
        )

    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(select(1))

    async def close(self) -> None:
        await self._engine.dispose()
