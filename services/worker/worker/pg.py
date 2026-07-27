"""Postgres implementation of the worker's ``Repo`` (build spec sections 4, 5).

Async SQLAlchemy Core over asyncpg. Table metadata mirrors
db/alembic/versions/0001_initial_schema.py (only the columns the worker reads
and writes).

The ``Repo`` protocol this satisfies — and the pure hash/statistics helpers it
shares with the in-memory implementations — live in ``worker.repository``,
which is kept free of SQLAlchemy so importing tier1/tier2 (as the local-mode
CLI does) never drags a database driver in.

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

from .repository import ledger_entry, welford_step
from .types import (
    AgentStat,
    AlertContext,
    BlameDraft,
    BreakerState,
    CheckRule,
    ClaimResult,
    EdgeRecord,
    GraphBundle,
    NodeScoreRow,
    OutputContract,
    PolicyDecision,
    PolicyRule,
    RunRecord,
    Tier1Verdict,
    Tier2Outcome,
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
    # Out-of-band artifact integrity record (migration 0006). Raw attribute
    # string; the worker parses it tolerantly (signals.parse_artifact_meta).
    Column("artifact_meta", Text),
    # Compact JSON digest of the run's TOOL spans (migration 0007). Raw
    # string; checks parse it tolerantly.
    Column("tool_calls", Text),
    # Tool schema fingerprint (migration 0009) — per-run identity for
    # version-diff views.
    Column("tool_schema_hash", Text),
    # Out-of-band declared contract params (migration 0011). Raw JSON-object
    # string; scoring parses it tolerantly.
    Column("contract_params", Text),
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
    # Terminal rubric split (migration 0010): the judge's FORM dimension
    # ({"verdict","requirement","observed","reasoning"}); the columns above are
    # CONTENT only. NULL on legacy single-verdict rows.
    Column("terminal_form", JSONB),
    Column("flags", JSONB),
    Column("flagged", Boolean, nullable=False),
    Column("sampled", Boolean, nullable=False),
    # Fingerprint of the rule set the deterministic verdict basis ran under
    # (migration 0008) — reconciliation provenance.
    Column("check_rules_hash", Text),
    # Worker judge-prompt fingerprint (migration 0009) — calibration slicing.
    # The judge MODEL is not recorded, a known limitation.
    Column("judge_prompt_hash", Text),
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
    # Worker judge-prompt fingerprint (migration 0009).
    Column("judge_prompt_hash", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

agent_stats = Table(
    "agent_stats",
    metadata,
    Column("agent_name", Text, primary_key=True),
    Column("graph_type", Text, primary_key=True),
    Column("tokens_out_mean", REAL),
    Column("tokens_out_std", REAL),
    Column("iterations_mean", REAL),
    Column("iterations_std", REAL),
    Column("sample_count", Integer),
    # Welford running-variance accumulators + cost baseline (migration 0007).
    Column("cost_mean", REAL),
    Column("cost_std", REAL),
    Column("tokens_out_m2", REAL),
    Column("cost_m2", REAL),
    Column("iterations_m2", REAL),
)

check_rules = Table(
    "check_rules",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_name", Text),
    Column("graph_type", Text),
    Column("kind", Text, nullable=False),
    Column("spec", JSONB, nullable=False),
)

output_contracts = Table(
    "output_contracts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_name", Text),
    Column("agent_version_pattern", Text),
    Column("json_schema", JSONB),
)

# Shadow policy gates (migration 0009, roadmap 2.2). Rules annotate — they
# never intercept; decisions record what WOULD have happened.
policy_rules = Table(
    "policy_rules",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False, unique=True),
    Column("predicate", JSONB, nullable=False),
    Column("action", Text, nullable=False),
    Column("shadow", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
)

policy_decisions = Table(
    "policy_decisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("graph_id", Uuid, nullable=False),
    Column("rule_name", Text, nullable=False),
    Column("decision", Text, nullable=False),
    Column("detail", Text),
    Column("mode", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

# Recorded circuit-breaker decisions (migration 0009, roadmap 2.3). A record,
# not an enforcement — enforcement happens only if the integration polls it.
breaker_state = Table(
    "breaker_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope_kind", Text, nullable=False),
    Column("scope_value", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("reason", Text),
    Column("opened_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    sa.UniqueConstraint("scope_kind", "scope_value", name="uq_breaker_state_scope"),
)

# Append-only evidence hash chain (migration 0009, roadmap 2.6).
evidence_ledger = Table(
    "evidence_ledger",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("report_id", Integer, nullable=False),
    Column("evidence_sha256", Text, nullable=False),
    Column("prev_hash", Text),
    Column("chain_hash", Text, nullable=False),
    Column("hmac_sig", Text, nullable=False),
)


class PgRepo:
    def __init__(self, engine: "object", audit_hmac_key: str = "dev-insecure-key") -> None:
        # The default key exists only so tests construct PgRepo without
        # settings; production (worker main) passes Settings.audit_hmac_key,
        # which MUST be overridden from its dev default there.
        self._engine = engine
        self._audit_hmac_key = audit_hmac_key

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
                artifact_meta=r.artifact_meta,
                tool_calls=r.tool_calls,
                tool_schema_hash=r.tool_schema_hash,
                contract_params=r.contract_params,
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
            terminal_form=verdict.terminal_form,
            flags=verdict.flags,
            flagged=verdict.flagged,
            sampled=verdict.sampled,
            check_rules_hash=verdict.check_rules_hash,
            judge_prompt_hash=verdict.judge_prompt_hash,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["graph_id"],
            set_={
                "terminal_judge_verdict": stmt.excluded.terminal_judge_verdict,
                "terminal_judge_score": stmt.excluded.terminal_judge_score,
                "terminal_judge_reasoning": stmt.excluded.terminal_judge_reasoning,
                "terminal_form": stmt.excluded.terminal_form,
                "flags": stmt.excluded.flags,
                "flagged": stmt.excluded.flagged,
                "sampled": stmt.excluded.sampled,
                "check_rules_hash": stmt.excluded.check_rules_hash,
                "judge_prompt_hash": stmt.excluded.judge_prompt_hash,
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
            terminal_form=dict(row.terminal_form) if row.terminal_form else None,
            flags=list(row.flags or []),
            flagged=row.flagged,
            sampled=row.sampled,
            check_rules_hash=row.check_rules_hash,
            judge_prompt_hash=row.judge_prompt_hash,
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
                cost_mean=row.cost_mean,
                cost_std=row.cost_std,
                tokens_out_m2=row.tokens_out_m2,
                cost_m2=row.cost_m2,
                iterations_m2=row.iterations_m2,
            )
            for row in rows
        }

    async def upsert_agent_stats(
        self,
        agent_name: str,
        graph_type: str,
        *,
        tokens_out: float | None,
        cost: float | None,
        iterations: float | None,
    ) -> None:
        async with self._engine.begin() as conn:
            # Upsert-then-lock: guarantee the row exists, then serialize
            # concurrent Welford steps on it with FOR UPDATE so no update is
            # lost (mean/m2 read-modify-write must not interleave).
            await conn.execute(
                pg_insert(agent_stats)
                .values(agent_name=agent_name, graph_type=graph_type, sample_count=0)
                .on_conflict_do_nothing(index_elements=["agent_name", "graph_type"])
            )
            row = (
                await conn.execute(
                    select(agent_stats)
                    .where(
                        agent_stats.c.agent_name == agent_name,
                        agent_stats.c.graph_type == graph_type,
                    )
                    .with_for_update()
                )
            ).first()
            n1 = (row.sample_count or 0) + 1
            values: dict[str, object] = {"sample_count": n1}
            # ONE Welford step per metric; None-valued metrics are skipped
            # and their accumulators stay untouched.
            if tokens_out is not None:
                mean, m2, std = welford_step(
                    n1, row.tokens_out_mean, row.tokens_out_m2, float(tokens_out)
                )
                values.update(tokens_out_mean=mean, tokens_out_m2=m2, tokens_out_std=std)
            if cost is not None:
                mean, m2, std = welford_step(n1, row.cost_mean, row.cost_m2, float(cost))
                values.update(cost_mean=mean, cost_m2=m2, cost_std=std)
            if iterations is not None:
                mean, m2, std = welford_step(
                    n1, row.iterations_mean, row.iterations_m2, float(iterations)
                )
                values.update(iterations_mean=mean, iterations_m2=m2, iterations_std=std)
            await conn.execute(
                sa.update(agent_stats)
                .where(
                    agent_stats.c.agent_name == agent_name,
                    agent_stats.c.graph_type == graph_type,
                )
                .values(**values)
            )

    async def read_check_rules(self) -> list[CheckRule]:
        stmt = select(
            check_rules.c.id,
            check_rules.c.agent_name,
            check_rules.c.graph_type,
            check_rules.c.kind,
            check_rules.c.spec,
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            CheckRule(
                id=row.id,
                agent_name=row.agent_name,
                graph_type=row.graph_type,
                kind=row.kind,
                spec=row.spec or {},
            )
            for row in rows
        ]

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
        supersede_others: bool = False,
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
                        # A new authoritative analysis landing back on this key
                        # means the problem is live again: a superseded incident
                        # (a graph near a judge boundary can oscillate between
                        # classifications and return to a key whose incident was
                        # graph-level superseded) or a resolved one reopens, so
                        # the fresh report is visible in the open list instead
                        # of hiding on a dead incident. Acknowledged stays — a
                        # human owns it.
                        set_={
                            "updated_at": func.now(),
                            "status": sa.case(
                                (
                                    incidents.c.status.in_(
                                        ("superseded", "resolved")
                                    ),
                                    "open",
                                ),
                                else_=incidents.c.status,
                            ),
                        },
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
                                judge_prompt_hash=blame.judge_prompt_hash,
                            )
                            .returning(blame_reports.c.id)
                        )
                    ).scalar_one()

                    # Evidence ledger (roadmap 2.6): append the hash-chain
                    # link in the SAME transaction that persists the report,
                    # so a report row and its ledger entry commit atomically.
                    # FOR UPDATE on the tail serializes concurrent appends —
                    # two writers cannot chain onto the same predecessor.
                    prev_row = (
                        await conn.execute(
                            select(evidence_ledger.c.chain_hash)
                            .order_by(evidence_ledger.c.id.desc())
                            .limit(1)
                            .with_for_update()
                        )
                    ).first()
                    prev_hash = prev_row.chain_hash if prev_row is not None else None
                    evidence_sha256, chain_hash, hmac_sig = ledger_entry(
                        blame.evidence, prev_hash, self._audit_hmac_key
                    )
                    await conn.execute(
                        pg_insert(evidence_ledger).values(
                            report_id=blame_report_id,
                            evidence_sha256=evidence_sha256,
                            prev_hash=prev_hash,
                            chain_hash=chain_hash,
                            hmac_sig=hmac_sig,
                        )
                    )

            if supersede_others:
                # The latest completed analysis is authoritative for its graph:
                # each analysis yields at most ONE classification, so any OTHER
                # live incident of this graph reflects an outdated verdict class
                # (e.g. degraded_quality before an escalation reclassified the
                # run to latent_defect, or any open incident after a re-analysis
                # came back clean). Leaving it open would page two stories about
                # one run. Resolved incidents are history and stay untouched.
                stale = (
                    sa.update(incidents)
                    .where(incidents.c.graph_id == graph_id)
                    .where(incidents.c.status.in_(("open", "acknowledged")))
                )
                if incident_id is not None:
                    stale = stale.where(incidents.c.id != incident_id)
                await conn.execute(
                    stale.values(status="superseded", updated_at=func.now())
                )

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

    async def read_policy_rules(self) -> list[PolicyRule]:
        stmt = select(policy_rules).where(policy_rules.c.enabled)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            PolicyRule(
                id=row.id,
                name=row.name,
                predicate=row.predicate or {},
                action=row.action,
                shadow=row.shadow,
                enabled=row.enabled,
            )
            for row in rows
        ]

    async def insert_policy_decisions(
        self, graph_id: UUID, decisions: list[PolicyDecision]
    ) -> None:
        if not decisions:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                pg_insert(policy_decisions),
                [
                    {
                        "graph_id": graph_id,
                        "rule_name": d.rule_name,
                        "decision": d.decision,
                        "detail": d.detail,
                        # Always shadow in v1: these rows record what WOULD
                        # have happened; nothing was actually blocked.
                        "mode": "shadow",
                    }
                    for d in decisions
                ],
            )

    async def upsert_breaker(
        self, scope_kind: str, scope_value: str, state: str, reason: str | None
    ) -> None:
        stmt = pg_insert(breaker_state).values(
            scope_kind=scope_kind,
            scope_value=scope_value,
            state=state,
            reason=reason,
            opened_at=func.now() if state == "open" else None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["scope_kind", "scope_value"],
            set_={
                "state": stmt.excluded.state,
                "reason": stmt.excluded.reason,
                "updated_at": func.now(),
                # Stamp opened_at exactly once per closed->open transition;
                # re-recording an already-open breaker keeps the original.
                "opened_at": sa.case(
                    (
                        (breaker_state.c.state != "open")
                        & (stmt.excluded.state == "open"),
                        func.now(),
                    ),
                    else_=breaker_state.c.opened_at,
                ),
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def read_breakers(self) -> list[BreakerState]:
        stmt = select(breaker_state)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            BreakerState(
                scope_kind=row.scope_kind,
                scope_value=row.scope_value,
                state=row.state,
                reason=row.reason,
            )
            for row in rows
        ]

    async def count_open_incidents_for_agent(self, agent_name: str) -> int:
        # One statement: live incidents x latest blame report x culprit runs.
        # ``run_id = ANY(culprit_run_ids)`` fans an incident out per culprit,
        # so count DISTINCT incident ids.
        stmt = (
            select(func.count(sa.distinct(incidents.c.id)))
            .select_from(
                incidents.join(
                    blame_reports,
                    (blame_reports.c.incident_id == incidents.c.id)
                    & (blame_reports.c.is_latest),
                ).join(
                    agent_runs,
                    blame_reports.c.culprit_run_ids.any(agent_runs.c.run_id),
                )
            )
            .where(incidents.c.status.in_(("open", "acknowledged")))
            .where(agent_runs.c.agent_name == agent_name)
        )
        async with self._engine.connect() as conn:
            return (await conn.execute(stmt)).scalar_one()

    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(select(1))

    async def close(self) -> None:
        await self._engine.dispose()
