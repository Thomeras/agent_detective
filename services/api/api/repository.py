"""Postgres repository: the API's only database touch point.

Routers depend on the `Repository` protocol; tests inject in-memory fakes
implementing the same surface (see services/api/tests/conftest.py). All
methods return plain mappings (or None) so routers stay serialization-only.
"""

from typing import Any, Mapping, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    agent_runs,
    blame_reports,
    breaker_state,
    edges,
    evidence_ledger,
    execution_graphs,
    ground_truth_labels,
    incidents,
    policy_decisions,
    tier1_verdicts,
)

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
    async def leaderboard_by_version(self) -> list[Row]: ...
    async def has_tier1_verdict(self, graph_id: UUID) -> bool: ...
    async def find_last_clean_graph(self, exclude_graph_id: UUID) -> Row | None: ...
    async def agent_version_stats(self, agent_name: str, agent_version: str) -> Row: ...
    async def list_policy_decisions(self, graph_id: UUID) -> list[Row]: ...
    async def insert_feedback(
        self, graph_id: UUID, label: str, culprit_run_id: UUID | None, note: str | None
    ) -> int: ...
    async def calibration_rows(self) -> list[Row]: ...
    async def list_breakers(self) -> list[Row]: ...
    async def list_ledger_rows(self) -> list[Row]: ...


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

    async def leaderboard_by_version(self) -> list[Row]:
        """Leaderboard grouped by the full version identity tuple (roadmap 2.1)."""
        total_cost = sa.func.coalesce(sa.func.sum(agent_runs.c.cost_usd), 0).label("total_cost_usd")
        run_count = sa.func.count().label("run_count")
        failure_rate = (
            sa.cast(sa.func.count().filter(agent_runs.c.status == "failed"), sa.Float) / sa.func.count()
        ).label("failure_rate")
        avg_score = sa.func.avg(agent_runs.c.quality_score).label("avg_quality_score")
        identity = [
            agent_runs.c.agent_name,
            agent_runs.c.agent_version,
            agent_runs.c.model_name,
            agent_runs.c.prompt_hash,
        ]
        stmt = (
            sa.select(*identity, total_cost, run_count, failure_rate, avg_score)
            .group_by(*identity)
            .order_by(total_cost.desc(), *identity)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def has_tier1_verdict(self, graph_id: UUID) -> bool:
        stmt = sa.select(sa.exists(tier1_verdicts).where(tier1_verdicts.c.graph_id == graph_id))
        async with self._session_factory() as session:
            return bool(await session.scalar(stmt))

    async def find_last_clean_graph(self, exclude_graph_id: UUID) -> Row | None:
        """Most recent OTHER finalized graph with zero incidents rows (frozen contract)."""
        stmt = (
            sa.select(execution_graphs)
            .where(
                execution_graphs.c.graph_id != exclude_graph_id,
                execution_graphs.c.status == "finalized",
                ~sa.exists(
                    sa.select(incidents.c.id).where(incidents.c.graph_id == execution_graphs.c.graph_id)
                ),
            )
            .order_by(
                sa.nullslast(execution_graphs.c.finalized_at.desc()),
                sa.nullslast(execution_graphs.c.started_at.desc()),
                execution_graphs.c.graph_id,
            )
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return row._mapping if row else None

    async def agent_version_stats(self, agent_name: str, agent_version: str) -> Row:
        """STATS block for the canary comparison (roadmap 2.4, tier1-only honesty).

        flag_rate / terminal_bad_rate are computed over graphs that HAVE a tier1
        verdict; when none do they are None, never a fabricated 0.0.
        """
        run_filter = sa.and_(
            agent_runs.c.agent_name == agent_name, agent_runs.c.agent_version == agent_version
        )
        graph_ids = sa.select(agent_runs.c.graph_id).where(run_filter)
        run_stmt = sa.select(
            sa.func.count().label("runs"),
            sa.func.count(sa.distinct(agent_runs.c.graph_id)).label("graphs"),
            sa.func.avg(agent_runs.c.quality_score).label("avg_quality"),
        ).where(run_filter)
        verdict_stmt = sa.select(
            sa.func.count().label("verdicts"),
            sa.func.count().filter(tier1_verdicts.c.flagged).label("flagged"),
            sa.func.count()
            .filter(tier1_verdicts.c.terminal_judge_verdict == "bad")
            .label("terminal_bad"),
        ).where(tier1_verdicts.c.graph_id.in_(graph_ids))
        incident_stmt = (
            sa.select(sa.func.count()).select_from(incidents).where(incidents.c.graph_id.in_(graph_ids))
        )
        async with self._session_factory() as session:
            run_row = (await session.execute(run_stmt)).first()._mapping
            verdict_row = (await session.execute(verdict_stmt)).first()._mapping
            incident_count = int(await session.scalar(incident_stmt) or 0)
        verdicts = int(verdict_row["verdicts"])
        avg_quality = run_row["avg_quality"]
        return {
            "agent_version": agent_version,
            "graphs": int(run_row["graphs"]),
            "runs": int(run_row["runs"]),
            "avg_quality": float(avg_quality) if avg_quality is not None else None,
            "flag_rate": (int(verdict_row["flagged"]) / verdicts) if verdicts else None,
            "terminal_bad_rate": (int(verdict_row["terminal_bad"]) / verdicts) if verdicts else None,
            "incidents": incident_count,
        }

    async def list_policy_decisions(self, graph_id: UUID) -> list[Row]:
        stmt = (
            sa.select(policy_decisions)
            .where(policy_decisions.c.graph_id == graph_id)
            .order_by(policy_decisions.c.created_at, policy_decisions.c.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def insert_feedback(
        self, graph_id: UUID, label: str, culprit_run_id: UUID | None, note: str | None
    ) -> int:
        stmt = (
            sa.insert(ground_truth_labels)
            .values(
                graph_id=graph_id,
                label=label,
                culprit_run_id=culprit_run_id,
                source="human",
                note=note,
            )
            .returning(ground_truth_labels.c.id)
        )
        async with self._session_factory() as session:
            label_id = await session.scalar(stmt)
            await session.commit()
            return int(label_id)

    async def calibration_rows(self) -> list[Row]:
        # LEFT JOIN keeps labels for graphs tier1 never judged: they land in the
        # NULL judge_prompt_hash slice with a NULL verdict (counts against recall).
        stmt = (
            sa.select(
                ground_truth_labels.c.graph_id,
                ground_truth_labels.c.label,
                tier1_verdicts.c.terminal_judge_verdict,
                tier1_verdicts.c.judge_prompt_hash,
            )
            .select_from(
                ground_truth_labels.outerjoin(
                    tier1_verdicts, tier1_verdicts.c.graph_id == ground_truth_labels.c.graph_id
                )
            )
            .order_by(ground_truth_labels.c.id)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def list_breakers(self) -> list[Row]:
        stmt = sa.select(breaker_state).order_by(breaker_state.c.scope_kind, breaker_state.c.scope_value)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]

    async def list_ledger_rows(self) -> list[Row]:
        # The whole chain, in insertion (id) order — verification walks it start to target.
        stmt = sa.select(evidence_ledger).order_by(evidence_ledger.c.id)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [row._mapping for row in result]
