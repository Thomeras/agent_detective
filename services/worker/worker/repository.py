"""The worker's persistence seam: the ``Repo`` protocol and its pure helpers.

Deliberately import-light — stdlib and ``worker.types`` only. tier1, tier2 and
the alerter import this module, so anything heavy here would become a hard
dependency of every consumer of the pipeline, including the local-mode CLI that
runs it with no database at all. The SQLAlchemy/asyncpg implementation lives in
``worker.pg`` (``PgRepo``); ``worker.main`` is the only module that imports it.

``ledger_entry`` and ``welford_step`` live here rather than beside the SQL
because both the Postgres repository and the in-memory ones must compute
identical hashes and identical rolling statistics — a fake that agreed with
itself but not with production would prove nothing.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import math
from typing import Protocol
from uuid import UUID

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


def ledger_entry(
    evidence: dict[str, object], prev_hash: str | None, hmac_key: str
) -> tuple[str, str, str]:
    """Compute one evidence-ledger link: ``(evidence_sha256, chain_hash, hmac_sig)``.

    The frozen algorithm (roadmap 2.6): sha256 over the canonically-serialized
    evidence (sorted keys, no whitespace, unescaped non-ASCII), chained onto
    the previous link's ``chain_hash`` (empty string for the first link), and
    signed with HMAC-SHA256 under ``hmac_key``. Shared by PgRepo and the
    in-memory test fake so both implement identical math. ``hmac_key`` MUST
    be overridden in production (Settings.audit_hmac_key) — signatures under
    the well-known dev default prove nothing.
    """
    canonical = json.dumps(
        evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    evidence_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    chain_hash = hashlib.sha256(
        ((prev_hash or "") + evidence_sha256).encode("utf-8")
    ).hexdigest()
    hmac_sig = hmac_mod.new(
        hmac_key.encode("utf-8"), chain_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return evidence_sha256, chain_hash, hmac_sig


def welford_step(
    n1: int, mean: float | None, m2: float | None, x: float
) -> tuple[float, float, float | None]:
    """One Welford running-variance update for a single metric.

    ``n1`` is the NEW sample count (old count + 1); ``mean``/``m2`` are the
    stored accumulators (None on the first sample). Returns
    ``(new_mean, new_m2, new_std)`` where ``new_std`` is
    ``sqrt(m2 / (n1 - 1))`` for n1 > 1 and None otherwise (a single sample
    has no variance). Shared by PgRepo and the in-memory test fake so both
    implement identical math.
    """
    mean0 = mean if mean is not None else 0.0
    m20 = m2 if m2 is not None else 0.0
    delta = x - mean0
    new_mean = mean0 + delta / n1
    new_m2 = m20 + delta * (x - new_mean)
    new_std = math.sqrt(new_m2 / (n1 - 1)) if n1 > 1 else None
    return new_mean, new_m2, new_std


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

    async def upsert_agent_stats(
        self,
        agent_name: str,
        graph_type: str,
        *,
        tokens_out: float | None,
        cost: float | None,
        iterations: float | None,
    ) -> None:
        """Fold ONE observed sample into the agent's baseline (Welford step).

        Metrics whose value is None are skipped (their accumulators stay
        untouched); ``sample_count`` advances once per call.
        """
        ...

    async def read_check_rules(self) -> list[CheckRule]: ...

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
        supersede_others: bool = False,
    ) -> Tier2Outcome: ...

    async def load_alert_context(self, incident_id: int) -> AlertContext | None: ...

    async def read_policy_rules(self) -> list[PolicyRule]:
        """Enabled shadow-gate rules only (disabled rules never evaluate)."""
        ...

    async def insert_policy_decisions(
        self, graph_id: UUID, decisions: list[PolicyDecision]
    ) -> None:
        """Record which rules WOULD have fired (mode='shadow', always)."""
        ...

    async def upsert_breaker(
        self, scope_kind: str, scope_value: str, state: str, reason: str | None
    ) -> None:
        """Record a breaker decision; ``opened_at`` is stamped once on the
        closed->open transition and preserved otherwise."""
        ...

    async def read_breakers(self) -> list[BreakerState]: ...

    async def count_open_incidents_for_agent(self, agent_name: str) -> int:
        """Count live (open/acknowledged) incidents whose latest blame report
        names a run of ``agent_name`` as culprit."""
        ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...
