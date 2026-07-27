"""In-memory implementations of the worker's persistence and transport seams.

The tier1/tier2 pipeline talks to Postgres, Redis and MinIO only through the
``Repo``, ``StreamPublisher`` and ``ObjectStore`` protocols. These are the
no-infrastructure implementations of those protocols: enough to run the real
pipeline in one process over one trace, which is what the local-mode CLI
(``detective analyze``) does and what the worker's own test suite runs against.

They are NOT a simplification of the database — they reproduce the idempotency
semantics the SQL relies on, because those semantics are what the pipeline's
correctness arguments rest on:

- ``tier1_verdicts`` keyed by ``graph_id`` (upsert, so a replay overwrites);
- the tier2 job claim behaves like ``ON CONFLICT (dedup_key) DO NOTHING`` — a
  second claim of the same key does not claim;
- incidents are unique per ``(graph_id, incident_key)`` and report whether the
  row was newly inserted;
- blame reports are versioned per incident with a single ``is_latest``;
- the evidence ledger chains with the same ``ledger_entry`` math PgRepo uses
  (imported, never reimplemented — a fake that agreed only with itself would
  prove nothing about the chain).

``upsert_agent_stats`` likewise calls the shared ``welford_step``, so baselines
computed locally match the ones the deployed service accumulates.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .repository import ledger_entry, welford_step
from .types import (
    AgentStat,
    AlertContext,
    BlameDraft,
    BreakerState,
    CheckRule,
    ClaimResult,
    GraphBundle,
    NodeScoreRow,
    OutputContract,
    PolicyDecision,
    PolicyRule,
    Tier1Verdict,
    Tier2Outcome,
)

# The dev-default HMAC key. Signatures under a well-known key prove nothing;
# the deployed service passes Settings.audit_hmac_key instead. Local analysis
# has no adversary to defend the chain against, so the default stands there.
DEV_AUDIT_HMAC_KEY = "dev-insecure-key"


class InMemoryRepo:
    """A ``Repo`` with Postgres-equivalent idempotency semantics, no database."""

    def __init__(self, audit_hmac_key: str = DEV_AUDIT_HMAC_KEY) -> None:
        self.bundles: dict[UUID, GraphBundle] = {}
        self.tier1: dict[UUID, Tier1Verdict] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.node_scores: dict[UUID, NodeScoreRow] = {}
        self.incidents: dict[tuple[UUID, str], dict[str, Any]] = {}
        self.blame_reports: list[dict[str, Any]] = []
        self.agent_stats: dict[str, AgentStat] = {}
        self.check_rules: list[CheckRule] = []
        self.contracts: list[OutputContract] = []
        self.policy_rules: list[PolicyRule] = []
        self.policy_decisions: list[dict[str, Any]] = []
        self.breakers: dict[tuple[str, str], dict[str, Any]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.audit_hmac_key = audit_hmac_key
        self._next_incident_id = 1
        self._next_blame_id = 1

    def add_bundle(self, bundle: GraphBundle) -> None:
        self.bundles[bundle.graph_id] = bundle

    async def load_graph(self, graph_id: UUID) -> GraphBundle | None:
        return self.bundles.get(graph_id)

    async def upsert_tier1_verdict(self, verdict: Tier1Verdict) -> None:
        self.tier1[verdict.graph_id] = verdict  # PK graph_id: idempotent overwrite

    async def read_tier1_verdict(self, graph_id: UUID) -> Tier1Verdict | None:
        return self.tier1.get(graph_id)

    async def claim_tier2_job(self, graph_id: UUID, dedup_key: str, trigger: str) -> ClaimResult:
        existing = self.jobs.get(dedup_key)
        if existing is not None:
            return ClaimResult(claimed=False, status=existing["status"])
        self.jobs[dedup_key] = {"graph_id": graph_id, "trigger": trigger, "status": "running"}
        return ClaimResult(claimed=True, status="running")

    async def fail_tier2_job(self, dedup_key: str, error: str) -> None:
        if dedup_key in self.jobs:
            self.jobs[dedup_key]["status"] = "failed"
            self.jobs[dedup_key]["error"] = error

    async def read_agent_stats(self, graph_type: str | None) -> dict[str, AgentStat]:
        return dict(self.agent_stats)

    async def upsert_agent_stats(
        self,
        agent_name: str,
        graph_type: str,
        *,
        tokens_out: float | None,
        cost: float | None,
        iterations: float | None,
    ) -> None:
        # Mirror of PgRepo.upsert_agent_stats: one shared sample_count bump
        # per call, one welford_step per non-None metric (same math — the
        # helper is imported from worker.repository, not reimplemented).
        prev = self.agent_stats.get(agent_name) or AgentStat(
            tokens_out_mean=None,
            tokens_out_std=None,
            iterations_mean=None,
            iterations_std=None,
            sample_count=0,
        )
        n1 = (prev.sample_count or 0) + 1
        updates: dict[str, Any] = {"sample_count": n1}
        if tokens_out is not None:
            mean, m2, std = welford_step(
                n1, prev.tokens_out_mean, prev.tokens_out_m2, float(tokens_out)
            )
            updates.update(tokens_out_mean=mean, tokens_out_m2=m2, tokens_out_std=std)
        if cost is not None:
            mean, m2, std = welford_step(n1, prev.cost_mean, prev.cost_m2, float(cost))
            updates.update(cost_mean=mean, cost_m2=m2, cost_std=std)
        if iterations is not None:
            mean, m2, std = welford_step(
                n1, prev.iterations_mean, prev.iterations_m2, float(iterations)
            )
            updates.update(iterations_mean=mean, iterations_m2=m2, iterations_std=std)
        self.agent_stats[agent_name] = replace(prev, **updates)

    async def read_check_rules(self) -> list[CheckRule]:
        return list(self.check_rules)

    async def read_output_contracts(self) -> list[OutputContract]:
        return list(self.contracts)

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
        for row in node_scores:
            self.node_scores[row.run_id] = row

        incident_id: int | None = None
        is_new = False
        blame_report_id: int | None = None

        if incident_key is not None and incident_trigger is not None:
            key = (graph_id, incident_key)
            existing = self.incidents.get(key)
            if existing is None:
                incident_id = self._next_incident_id
                self._next_incident_id += 1
                self.incidents[key] = {
                    "id": incident_id,
                    "graph_id": graph_id,
                    "incident_key": incident_key,
                    "trigger": incident_trigger,
                    "status": "open",
                }
                is_new = True
            else:
                incident_id = existing["id"]
                is_new = False
                # Mirror of the pg ON CONFLICT status CASE: a new authoritative
                # analysis landed back on this key, so a superseded/resolved
                # incident reopens; acknowledged stays — a human owns it.
                if existing["status"] in ("superseded", "resolved"):
                    existing["status"] = "open"

            if blame is not None:
                versions = [
                    b["version"] for b in self.blame_reports if b["incident_id"] == incident_id
                ]
                next_version = (max(versions) if versions else 0) + 1
                for b in self.blame_reports:
                    if b["incident_id"] == incident_id:
                        b["is_latest"] = False
                blame_report_id = self._next_blame_id
                self._next_blame_id += 1
                self.blame_reports.append(
                    {
                        "id": blame_report_id,
                        "incident_id": incident_id,
                        "graph_id": graph_id,
                        "version": next_version,
                        "is_latest": True,
                        "report_type": blame.report_type,
                        "culprit_run_ids": blame.culprit_run_ids,
                        "propagation_path": blame.propagation_path,
                        "confidence": blame.confidence,
                        "downstream_cost_usd": blame.downstream_cost_usd,
                        "unscored_run_ids": blame.unscored_run_ids,
                        "evidence": blame.evidence,
                        "judge_prompt_hash": blame.judge_prompt_hash,
                    }
                )
                # Evidence-ledger mirror (same math as PgRepo via
                # ledger_entry): chain onto the globally last link.
                prev_hash = self.ledger[-1]["chain_hash"] if self.ledger else None
                evidence_sha256, chain_hash, hmac_sig = ledger_entry(
                    blame.evidence, prev_hash, self.audit_hmac_key
                )
                self.ledger.append(
                    {
                        "report_id": blame_report_id,
                        "evidence_sha256": evidence_sha256,
                        "prev_hash": prev_hash,
                        "chain_hash": chain_hash,
                        "hmac_sig": hmac_sig,
                    }
                )

        if supersede_others:
            # Mirror of the SQL semantics: the latest completed analysis is
            # authoritative for its graph — other live incidents reflect an
            # outdated classification and are superseded (resolved stay history).
            for inc in self.incidents.values():
                if (
                    inc["graph_id"] == graph_id
                    and inc["id"] != incident_id
                    and inc["status"] in ("open", "acknowledged")
                ):
                    inc["status"] = "superseded"

        if dedup_key in self.jobs:
            self.jobs[dedup_key]["status"] = "done"
        return Tier2Outcome(
            incident_id=incident_id, is_new=is_new, blame_report_id=blame_report_id
        )

    async def load_alert_context(self, incident_id: int) -> AlertContext | None:
        incident = next(
            (i for i in self.incidents.values() if i["id"] == incident_id), None
        )
        if incident is None:
            return None
        report = next(
            (
                b
                for b in self.blame_reports
                if b["incident_id"] == incident_id and b["is_latest"]
            ),
            None,
        )
        return AlertContext(
            incident_id=incident_id,
            graph_id=incident["graph_id"],
            trigger=incident["trigger"],
            report_type=report["report_type"] if report else None,
            culprit_run_ids=report["culprit_run_ids"] if report else [],
            confidence=report["confidence"] if report else None,
            downstream_cost_usd=report["downstream_cost_usd"] if report else None,
        )

    async def read_policy_rules(self) -> list[PolicyRule]:
        return [r for r in self.policy_rules if r.enabled]

    async def insert_policy_decisions(
        self, graph_id: UUID, decisions: list[PolicyDecision]
    ) -> None:
        for d in decisions:
            self.policy_decisions.append(
                {
                    "graph_id": graph_id,
                    "rule_name": d.rule_name,
                    "decision": d.decision,
                    "detail": d.detail,
                    "mode": "shadow",
                }
            )

    async def upsert_breaker(
        self, scope_kind: str, scope_value: str, state: str, reason: str | None
    ) -> None:
        key = (scope_kind, scope_value)
        now = datetime.now(timezone.utc)
        prev = self.breakers.get(key)
        # opened_at is stamped exactly once per closed->open transition
        # (mirror of the SQL CASE in PgRepo.upsert_breaker).
        if state == "open" and (prev is None or prev["state"] != "open"):
            opened_at = now
        else:
            opened_at = prev["opened_at"] if prev else None
        self.breakers[key] = {
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "state": state,
            "reason": reason,
            "opened_at": opened_at,
            "updated_at": now,
        }

    async def read_breakers(self) -> list[BreakerState]:
        return [
            BreakerState(
                scope_kind=b["scope_kind"],
                scope_value=b["scope_value"],
                state=b["state"],
                reason=b["reason"],
            )
            for b in self.breakers.values()
        ]

    async def count_open_incidents_for_agent(self, agent_name: str) -> int:
        # Mirror of the PgRepo join: live incidents x latest blame report x
        # culprit runs' agent_name (runs looked up across all bundles).
        run_agent: dict[UUID, str | None] = {}
        for bundle in self.bundles.values():
            for run in bundle.runs:
                run_agent[run.run_id] = run.agent_name
        count = 0
        for inc in self.incidents.values():
            if inc["status"] not in ("open", "acknowledged"):
                continue
            latest = next(
                (
                    b
                    for b in self.blame_reports
                    if b["incident_id"] == inc["id"] and b["is_latest"]
                ),
                None,
            )
            if latest is None:
                continue
            if any(
                run_agent.get(rid) == agent_name for rid in latest["culprit_run_ids"]
            ):
                count += 1
        return count

    async def ping(self) -> None:
        pass

    async def close(self) -> None:
        pass


class InMemoryObjectStore:
    """An ``ObjectStore`` backed by a dict.

    Local analysis keeps every payload inline (there is no size pressure and no
    bucket to overflow into), so this normally stays empty; it exists so
    ``resolve_payload`` has something to call when a bundle was built from rows
    that DO carry an overflow ref.
    """

    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None) -> None:
        self.objects: dict[tuple[str, str], bytes] = objects or {}

    async def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]


class CollectingPublisher:
    """A ``StreamPublisher`` that records messages instead of sending them.

    In the deployed system tier1 hands off to tier2 through a Redis stream. In
    local mode there is no broker, so the caller reads what tier1 published and
    decides whether to run tier2 — same message, same contract, no queue.
    """

    def __init__(self) -> None:
        self.published: dict[str, list[dict[str, Any]]] = {}
        self._id = 0

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str:
        self.published.setdefault(stream, []).append(message)
        self._id += 1
        return f"0-{self._id}"

    def messages(self, stream: str) -> list[dict[str, Any]]:
        return self.published.get(stream, [])

    async def ping(self) -> None:
        pass

    async def close(self) -> None:
        pass
