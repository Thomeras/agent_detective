"""Shared test harness: in-memory fakes for every worker seam.

The suite is fully network-free. Fakes implement the same async protocols as the
real clients (Repo, ObjectStore, StreamPublisher/StreamConsumer, JudgeClient,
WebhookClient) and emulate Postgres idempotency semantics (ON CONFLICT job
claim, ``(graph_id, incident_key)`` uniqueness, versioned blame reports) so
tests assert on the state the real database would hold.

IMPORTANT (repo-wide gotcha): all service source roots share one ``.pth`` on
sys.path, so ``from tests.conftest import ...`` resolves to the wrong service.
Test modules must import these helpers as ``from conftest import ...`` and be
run from within the worker directory.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from uuid import UUID

from blame_engine import NodeScore

from worker.config import Settings
from worker.judge_client import JudgeError
from worker.repository import ledger_entry, welford_step
from worker.types import (
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
    StreamMessage,
    Tier1Verdict,
    Tier2Outcome,
)


def uid(n: int) -> UUID:
    """Deterministic UUID for a small integer (e.g. uid(1))."""
    return UUID(int=n)


def make_run(
    run_id: int,
    agent_name: str,
    *,
    graph_id: int = 1,
    status: str = "ok",
    input_inline: str | None = "input",
    output_inline: str | None = "a well formed output",
    agent_version: str | None = None,
    cost_usd: float | None = 0.0,
    tokens_out: int | None = None,
    end_time: float = 0.0,
    artifact_meta: str | None = None,
    tool_calls: str | None = None,
    tool_schema_hash: str | None = None,
) -> RunRecord:
    from datetime import datetime, timezone

    ended = datetime.fromtimestamp(end_time, tz=timezone.utc) if end_time else None
    return RunRecord(
        run_id=uid(run_id),
        graph_id=uid(graph_id),
        agent_name=agent_name,
        agent_version=agent_version,
        status=status,
        input_inline=input_inline,
        input_overflow_ref=None,
        output_inline=output_inline,
        output_overflow_ref=None,
        output_bytes=len(output_inline.encode()) if output_inline else 0,
        cost_usd=cost_usd,
        tokens_in=None,
        tokens_out=tokens_out,
        started_at=None,
        ended_at=ended,
        artifact_meta=artifact_meta,
        tool_calls=tool_calls,
        tool_schema_hash=tool_schema_hash,
    )


def make_bundle(
    runs: list[RunRecord],
    edges: list[tuple[int, int]],
    *,
    graph_id: int = 1,
    name: str | None = "test graph",
    graph_type: str | None = "pipeline",
    total_cost_usd: float | None = None,
    edge_type: str = "SPAWN",
) -> GraphBundle:
    return GraphBundle(
        graph_id=uid(graph_id),
        name=name,
        graph_type=graph_type,
        total_cost_usd=total_cost_usd,
        run_count=len(runs),
        runs=runs,
        edges=[EdgeRecord(from_run_id=uid(a), to_run_id=uid(b), type=edge_type) for a, b in edges],
    )


class FakeRepo:
    """In-memory Repo with Postgres-equivalent idempotency semantics."""

    def __init__(self) -> None:
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
        # Evidence-ledger mirror: same chain algorithm as PgRepo
        # (ledger_entry is imported, not reimplemented), dev default key.
        self.ledger: list[dict[str, Any]] = []
        self.audit_hmac_key = "dev-insecure-key"
        self._next_incident_id = 1
        self._next_blame_id = 1
        self.fail_ping = False

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
        from datetime import datetime, timezone

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
        if self.fail_ping:
            raise ConnectionError("postgres unreachable")

    async def close(self) -> None:
        pass


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]


class FakeStreams:
    """Implements both StreamPublisher and StreamConsumer protocols."""

    def __init__(self) -> None:
        self.published: dict[str, list[dict[str, Any]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self._feed: dict[str, list[StreamMessage]] = {}
        self.acked: dict[str, list[str]] = {}
        self.pending_rows: dict[str, list[Any]] = {}
        self._id = 0

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str:
        self.published.setdefault(stream, []).append(message)
        self._id += 1
        return f"0-{self._id}"

    def messages(self, stream: str) -> list[dict[str, Any]]:
        return self.published.get(stream, [])

    def feed(self, stream: str, data: dict[str, Any]) -> str:
        self._id += 1
        msg = StreamMessage(id=f"0-{self._id}", data=data)
        self._feed.setdefault(stream, []).append(msg)
        return msg.id

    async def ensure_group(self, stream: str, group: str) -> None:
        self.groups.add((stream, group))

    async def read(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[StreamMessage]:
        messages = self._feed.pop(stream, [])
        return messages[:count]

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        self.acked.setdefault(stream, []).append(message_id)

    async def pending(self, stream, group, min_idle_ms, count):
        return self.pending_rows.get(stream, [])

    async def claim(self, stream, group, consumer, message_id, min_idle_ms):
        return StreamMessage(id=message_id, data={"payload": "poison"})

    async def ping(self) -> None:
        pass

    async def close(self) -> None:
        pass


_AGENT_RE = re.compile(r"agent named `([^`]+)`")


class FakeJudge:
    """Canned judge keyed on prompt content.

    - per-node judge prompt ("quality judge for a single step") -> the mapping
      in ``node_verdicts[agent_name]`` (default task_score 1.0);
    - terminal judge prompt ("final quality gate") -> ``terminal``;
    - claims prompt ("auditing one step") -> ``{"claims": claims}``.

    Set ``fail=True`` to always raise (drives the "judge -> None" paths).
    """

    def __init__(
        self,
        *,
        node_verdicts: dict[str, dict[str, Any]] | None = None,
        terminal: dict[str, Any] | None = None,
        claims: list[str] | None = None,
        fail: bool = False,
    ) -> None:
        self.node_verdicts = node_verdicts or {}
        self.terminal = terminal or {"verdict": "ok", "score": 0.95, "reasoning": "looks good"}
        self.claims = claims or []
        self.fail = fail
        self.calls: list[str] = []

    async def complete_json(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        if self.fail:
            raise JudgeError("forced failure")
        if "final quality gate" in prompt:
            self.calls.append("terminal")
            return dict(self.terminal)
        if "auditing one step" in prompt:
            self.calls.append("claims")
            return {"claims": list(self.claims)}
        # per-node judge
        match = _AGENT_RE.search(prompt)
        agent = match.group(1) if match else "unknown"
        self.calls.append(f"judge:{agent}")
        return dict(
            self.node_verdicts.get(
                agent, {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"}
            )
        )


class FakeWebhook:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, payload: dict[str, Any]) -> None:
        self.posts.append((url, payload))


def make_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


def node_score(run_id: int, score: float | None, **kw: Any) -> NodeScore:
    return NodeScore(
        run_id=str(uid(run_id)),
        score=score,
        components=kw.get("components", {"schema": None, "judge": score, "heuristics": None}),
        input_flawed=kw.get("input_flawed"),
        unscored_reason=kw.get("unscored_reason"),
        judge_note=kw.get("judge_note"),
    )
