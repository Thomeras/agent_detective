"""Postgres repository: the API's only database touch point.

Routers depend on the `Repository` protocol; tests inject in-memory fakes
implementing the same surface (see services/api/tests/conftest.py). All
methods return plain mappings (or None) so routers stay serialization-only.
"""

from typing import Any, Mapping, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import agent_runs, blame_reports, edges, execution_graphs, incidents, tier1_verdicts

Row = Mapping[str, Any]


class Repository(Protocol):
    async def list_graphs(self, limit: int, offset: int) -> list[Row]: ...
    async def get_graph(self, graph_id: UUID) -> Row | None: ...
    async def list_runs(self, graph_id: UUID) -> list[Row]: ...
    async def list_edges(self, graph_id: UUID) -> list[Row]: ...
    async def get_run(self, graph_id: UUID, run_id: UUID) -> Row | None: ...
    async def list_incidents(self, limit: int, offset: int) -> list[Row]: ...
    async def get_incident(self, incident_id: int) -> Row | None: ...
    async def get_latest_report(self, incident_id: int) -> Row | None: ...
    async def update_incident_status(self, incident_id: int, status: str) -> Row | None: ...
    async def leaderboard(self) -> list[Row]: ...
    async def has_tier1_verdict(self, graph_id: UUID) -> bool: ...


class SqlRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def list_graphs(self, limit: int, offset: int) -> list[Row]:
        stmt = (
            sa.select(execution_graphs)
            .order_by(sa.nullslast(execution_graphs.c.started_at.desc()), execution_graphs.c.graph_id)
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def get_graph(self, graph_id: UUID) -> Row | None:
        stmt = sa.select(execution_graphs).where(execution_graphs.c.graph_id == graph_id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return row._mapping if row else None

    async def list_runs(self, graph_id: UUID) -> list[Row]:
        stmt = (
            sa.select(agent_runs)
            .where(agent_runs.c.graph_id == graph_id)
            .order_by(sa.nullslast(agent_runs.c.started_at), agent_runs.c.run_id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def list_edges(self, graph_id: UUID) -> list[Row]:
        stmt = sa.select(edges).where(edges.c.graph_id == graph_id).order_by(edges.c.id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def get_run(self, graph_id: UUID, run_id: UUID) -> Row | None:
        stmt = sa.select(agent_runs).where(
            agent_runs.c.graph_id == graph_id, agent_runs.c.run_id == run_id
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return row._mapping if row else None

    async def list_incidents(self, limit: int, offset: int) -> list[Row]:
        # Inbox rows joined with the latest blame report summary (if any).
        stmt = (
            sa.select(
                incidents.c.id,
                incidents.c.graph_id,
                incidents.c.incident_key,
                incidents.c.trigger,
                incidents.c.status,
                incidents.c.created_at,
                incidents.c.updated_at,
                blame_reports.c.id.label("report_id"),
                blame_reports.c.report_type,
                blame_reports.c.culprit_run_ids,
                blame_reports.c.confidence,
                blame_reports.c.downstream_cost_usd,
            )
            .select_from(
                incidents.outerjoin(
                    blame_reports,
                    sa.and_(blame_reports.c.incident_id == incidents.c.id, blame_reports.c.is_latest),
                )
            )
            .order_by(incidents.c.created_at.desc(), incidents.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def get_incident(self, incident_id: int) -> Row | None:
        stmt = sa.select(incidents).where(incidents.c.id == incident_id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return row._mapping if row else None

    async def get_latest_report(self, incident_id: int) -> Row | None:
        stmt = (
            sa.select(blame_reports)
            .where(blame_reports.c.incident_id == incident_id, blame_reports.c.is_latest)
            .order_by(blame_reports.c.version.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return row._mapping if row else None

    async def update_incident_status(self, incident_id: int, status: str) -> Row | None:
        stmt = (
            sa.update(incidents)
            .where(incidents.c.id == incident_id)
            .values(status=status, updated_at=sa.func.now())
            .returning(incidents)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            await session.commit()
            return row._mapping if row else None

    async def leaderboard(self) -> list[Row]:
        total_cost = sa.func.coalesce(sa.func.sum(agent_runs.c.cost_usd), 0).label("total_cost_usd")
        run_count = sa.func.count().label("run_count")
        failure_rate = (
            sa.cast(sa.func.count().filter(agent_runs.c.status == "failed"), sa.Float) / sa.func.count()
        ).label("failure_rate")
        avg_score = sa.func.avg(agent_runs.c.quality_score).label("avg_quality_score")
        stmt = (
            sa.select(agent_runs.c.agent_name, total_cost, run_count, failure_rate, avg_score)
            .group_by(agent_runs.c.agent_name)
            .order_by(total_cost.desc(), agent_runs.c.agent_name)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def has_tier1_verdict(self, graph_id: UUID) -> bool:
        stmt = sa.select(sa.exists(tier1_verdicts).where(tier1_verdicts.c.graph_id == graph_id))
        async with self._session_factory() as session:
            return bool(await session.scalar(stmt))
