"""Postgres repository: graph/run/edge upserts and finalizer queries.

Writes are idempotent by construction (build spec 4.1: redelivery is a
no-op):

- graphs: INSERT ... ON CONFLICT DO UPDATE with LEAST/GREATEST on the time
  bounds (both ignore NULLs in Postgres, so re-applying the same batch
  changes nothing) and a count-based run_count refresh;
- runs: INSERT ... ON CONFLICT DO NOTHING (uuid5 keys make the same span
  land on the same run_id);
- edges: INSERT ... ON CONFLICT DO NOTHING on the natural unique key.

The ``Repo`` protocol is the test seam; ``PgRepo`` is the SQLAlchemy/asyncpg
implementation. Table metadata mirrors db/alembic/versions/0001_initial_schema.py
plus later additive migrations (only the columns ingest manages;
tool_schema_hash arrives with migration 0009).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import (
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
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .types import EdgeRow, FinalizeResult, GraphActivity, IngestBatch

metadata = MetaData()

execution_graphs = Table(
    "execution_graphs",
    metadata,
    Column("graph_id", Uuid, primary_key=True),
    Column("name", Text),
    Column("graph_type", Text),
    Column("status", Text, nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
    Column("finalized_at", DateTime(timezone=True)),
    Column("total_cost_usd", Numeric),
    Column("run_count", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("run_id", Uuid, primary_key=True),
    Column("graph_id", Uuid, nullable=False),
    Column("agent_name", Text),
    Column("agent_version", Text),
    Column("model_name", Text),
    Column("prompt_hash", Text),
    Column("tool_schema_hash", Text),
    Column("artifact_meta", Text),
    Column("tool_calls", Text),
    Column("contract_params", Text),
    Column("attempt", Integer),
    Column("attempt_of", Text),
    Column("parent_run_id", Uuid),
    Column("trace_id", Text),
    Column("status", Text, nullable=False),
    Column("input_inline", Text),
    Column("input_overflow_ref", Text),
    Column("input_bytes", Integer),
    Column("output_inline", Text),
    Column("output_overflow_ref", Text),
    Column("output_bytes", Integer),
    Column("input_summary", Text),
    Column("output_summary", Text),
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
    Column("detection_method", Text),
)


class Repo(Protocol):
    """Persistence seam; faked by an in-memory implementation in tests."""

    async def upsert_batch(self, batch: IngestBatch, *, refresh_runs: bool = False) -> None:
        """Idempotent write of one mapped batch.

        With ``refresh_runs`` the batch is authoritative for run DATA: run rows
        are updated in place instead of first-write-wins. Only the finalization
        re-map sets it — that batch is mapped from the graph's full span set,
        so it is at least as informed as any per-request row.
        """
        ...

    async def trace_ids_for_graph(self, graph_id: UUID) -> list[str]:
        """Distinct trace ids of the graph's runs (re-map span lookup)."""
        ...

    async def list_active_graph_activity(self) -> list[GraphActivity]: ...

    async def finalize_graph(self, graph_id: UUID, finalized_at: datetime) -> FinalizeResult | None:
        """Atomically finalize one graph.

        Returns the finalize outcome, or None when the graph was not active
        (already finalized or unknown) so callers only publish the stream
        message once.
        """
        ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class PgRepo:
    def __init__(self, engine: "object") -> None:
        self._engine = engine

    async def upsert_batch(self, batch: IngestBatch, *, refresh_runs: bool = False) -> None:
        async with self._engine.begin() as conn:
            for graph in batch.graphs:
                stmt = pg_insert(execution_graphs).values(
                    graph_id=graph.graph_id,
                    graph_type=graph.graph_type,
                    status="active",
                    started_at=graph.started_at,
                    ended_at=graph.ended_at,
                    run_count=0,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["graph_id"],
                    # LEAST/GREATEST ignore NULLs: keep the widest known time
                    # bounds; never touch status/finalized_at of a finalized graph.
                    set_={
                        "started_at": func.least(
                            execution_graphs.c.started_at, stmt.excluded.started_at
                        ),
                        "ended_at": func.greatest(
                            execution_graphs.c.ended_at, stmt.excluded.ended_at
                        ),
                        # Keep the first-known cohort key: a later batch missing
                        # service.name must not clobber it back to NULL.
                        "graph_type": func.coalesce(
                            execution_graphs.c.graph_type, stmt.excluded.graph_type
                        ),
                    },
                )
                await conn.execute(stmt)

            for run in batch.runs:
                stmt = (
                    pg_insert(agent_runs)
                    .values(
                        run_id=run.run_id,
                        graph_id=run.graph_id,
                        agent_name=run.agent_name,
                        agent_version=run.agent_version,
                        model_name=run.model_name,
                        prompt_hash=run.prompt_hash,
                        tool_schema_hash=run.tool_schema_hash,
                        artifact_meta=run.artifact_meta,
                        tool_calls=run.tool_calls,
                        contract_params=run.contract_params,
                        attempt=run.attempt,
                        attempt_of=run.attempt_of,
                        trace_id=run.trace_id,
                        status=run.status,
                        input_inline=run.input_inline,
                        input_overflow_ref=run.input_overflow_ref,
                        input_bytes=run.input_bytes,
                        output_inline=run.output_inline,
                        output_overflow_ref=run.output_overflow_ref,
                        output_bytes=run.output_bytes,
                        input_summary=run.input_summary,
                        output_summary=run.output_summary,
                        cost_usd=run.cost_usd,
                        tokens_in=run.tokens_in,
                        tokens_out=run.tokens_out,
                        started_at=run.started_at,
                        ended_at=run.ended_at,
                    )
                )
                if refresh_runs:
                    # Full-span-set re-map: refresh every ingest-owned data
                    # column. Scoring columns belong to the worker and are not
                    # in this statement, so they survive untouched.
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["run_id"],
                        set_={
                            c: getattr(stmt.excluded, c)
                            for c in (
                                "graph_id", "agent_name", "agent_version",
                                "model_name", "prompt_hash", "tool_schema_hash",
                                "artifact_meta", "tool_calls", "contract_params",
                                "attempt", "attempt_of",
                                "trace_id",
                                "status", "input_inline", "input_overflow_ref",
                                "input_bytes", "output_inline",
                                "output_overflow_ref", "output_bytes",
                                "input_summary", "output_summary", "cost_usd",
                                "tokens_in", "tokens_out", "started_at",
                                "ended_at",
                            )
                        },
                    )
                else:
                    stmt = stmt.on_conflict_do_nothing()
                await conn.execute(stmt)

            for edge in batch.edges:
                stmt = (
                    pg_insert(edges)
                    .values(
                        graph_id=edge.graph_id,
                        from_run_id=edge.from_run_id,
                        to_run_id=edge.to_run_id,
                        type=edge.type,
                        detection_method=edge.detection_method,
                    )
                    .on_conflict_do_nothing()
                )
                await conn.execute(stmt)

            # run_count is derived, not incremented: recomputing the count is
            # idempotent under redelivery (no double bump on retries).
            count = (
                select(func.count(agent_runs.c.run_id))
                .where(agent_runs.c.graph_id == execution_graphs.c.graph_id)
                .scalar_subquery()
            )
            for graph_id in batch.graph_ids:
                await conn.execute(
                    update(execution_graphs)
                    .where(execution_graphs.c.graph_id == graph_id)
                    .values(run_count=count)
                )

    async def trace_ids_for_graph(self, graph_id: UUID) -> list[str]:
        stmt = (
            select(agent_runs.c.trace_id)
            .where(agent_runs.c.graph_id == graph_id)
            .distinct()
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return sorted(row.trace_id for row in rows if row.trace_id)

    async def list_active_graph_activity(self) -> list[GraphActivity]:
        activity_stmt = (
            select(
                execution_graphs.c.graph_id,
                func.max(func.greatest(agent_runs.c.started_at, agent_runs.c.ended_at)).label(
                    "last_activity"
                ),
                execution_graphs.c.created_at,
            )
            .select_from(
                execution_graphs.outerjoin(
                    agent_runs, agent_runs.c.graph_id == execution_graphs.c.graph_id
                )
            )
            .where(execution_graphs.c.status == "active")
            .group_by(execution_graphs.c.graph_id, execution_graphs.c.created_at)
        )
        # A root run is a run with no incoming edge within its graph.
        incoming = (
            select(edges.c.id)
            .where(
                edges.c.graph_id == agent_runs.c.graph_id,
                edges.c.to_run_id == agent_runs.c.run_id,
            )
            .exists()
        )
        root_ended_stmt = (
            select(agent_runs.c.graph_id)
            .select_from(
                agent_runs.join(
                    execution_graphs,
                    (execution_graphs.c.graph_id == agent_runs.c.graph_id)
                    & (execution_graphs.c.status == "active"),
                )
            )
            .where(agent_runs.c.ended_at.is_not(None), ~incoming)
        )
        async with self._engine.connect() as conn:
            activity = (await conn.execute(activity_stmt)).all()
            root_ended = {row.graph_id for row in (await conn.execute(root_ended_stmt)).all()}
        return [
            GraphActivity(
                graph_id=row.graph_id,
                last_activity=row.last_activity,
                created_at=row.created_at,
                root_ended=row.graph_id in root_ended,
            )
            for row in activity
        ]

    async def finalize_graph(self, graph_id: UUID, finalized_at: datetime) -> FinalizeResult | None:
        count = (
            select(func.count(agent_runs.c.run_id))
            .where(agent_runs.c.graph_id == execution_graphs.c.graph_id)
            .scalar_subquery()
        )
        # No coalesce: SUM over all-NULL costs must stay NULL. Folding it to 0
        # made every uninstrumented graph report a confident "$0" — the UI has
        # no way to tell that apart from a graph that genuinely cost nothing,
        # and "we did not measure" is not a measurement of zero.
        total_cost = (
            select(func.sum(agent_runs.c.cost_usd))
            .where(agent_runs.c.graph_id == execution_graphs.c.graph_id)
            .scalar_subquery()
        )
        stmt = (
            update(execution_graphs)
            .where(
                execution_graphs.c.graph_id == graph_id,
                # The status guard makes a double finalize a no-op: the second
                # call matches no row and returns None.
                execution_graphs.c.status == "active",
            )
            .values(
                status="finalized",
                finalized_at=finalized_at,
                run_count=count,
                total_cost_usd=total_cost,
            )
            .returning(execution_graphs.c.run_count)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            return None
        return FinalizeResult(graph_id=graph_id, finalized_at=finalized_at, run_count=row.run_count)

    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(select(1))

    async def close(self) -> None:
        await self._engine.dispose()
