"""Deploy-vitals repo plumbing over the in-memory FakeRepo (migration 0009).

Pure FakeRepo tests: the evidence-ledger hash chain, breaker upsert
transitions, live-incident counting per culprit agent, and enabled-only
policy rule reads. The ledger assertions recompute every hash independently
with hashlib/hmac so they pin the FROZEN algorithm, not merely FakeRepo's
agreement with itself.
"""

import asyncio
import hashlib
import hmac
import json
from uuid import UUID

from worker.types import BlameDraft, PolicyRule

from conftest import FakeRepo, make_bundle, make_run, uid


def make_blame(culprit: int, evidence: dict) -> BlameDraft:
    return BlameDraft(
        report_type="root_cause",
        culprit_run_ids=[uid(culprit)],
        propagation_path=[uid(culprit)],
        confidence=0.9,
        downstream_cost_usd=0.0,
        unscored_run_ids=[],
        evidence=evidence,
    )


def persist(
    repo: FakeRepo,
    *,
    graph_id: int,
    incident_key: str = "k",
    blame: BlameDraft | None = None,
) -> None:
    asyncio.run(
        repo.persist_tier2_result(
            dedup_key=f"dk-{graph_id}-{incident_key}",
            node_scores=[],
            graph_id=uid(graph_id),
            incident_key=incident_key,
            incident_trigger="tier1_flagged",
            blame=blame,
        )
    )


def frozen_link(evidence: dict, prev_hash: str | None) -> tuple[str, str, str]:
    """The frozen ledger algorithm, restated independently of worker code."""
    canonical = json.dumps(
        evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    evidence_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    chain_hash = hashlib.sha256(
        ((prev_hash or "") + evidence_sha256).encode("utf-8")
    ).hexdigest()
    hmac_sig = hmac.new(
        b"dev-insecure-key", chain_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return evidence_sha256, chain_hash, hmac_sig


def test_ledger_two_appends_chain_and_hmac_verify():
    repo = FakeRepo()
    # Non-ASCII on purpose: canonical form uses ensure_ascii=False.
    ev1 = {"b": 1, "a": "příliš žluťoučký"}
    ev2 = {"z": [1, 2], "m": None}
    persist(repo, graph_id=1, blame=make_blame(1, ev1))
    persist(repo, graph_id=2, blame=make_blame(2, ev2))

    assert len(repo.ledger) == 2
    first, second = repo.ledger

    # Genesis link chains onto the empty string.
    sha1, chain1, sig1 = frozen_link(ev1, None)
    assert first["prev_hash"] is None
    assert first["evidence_sha256"] == sha1
    assert first["chain_hash"] == chain1
    assert first["hmac_sig"] == sig1

    # Second link chains onto the first's chain_hash.
    assert second["prev_hash"] == first["chain_hash"]
    sha2, chain2, sig2 = frozen_link(ev2, first["chain_hash"])
    assert second["evidence_sha256"] == sha2
    assert second["chain_hash"] == chain2
    # hmac verifies against the dev key.
    assert hmac.compare_digest(second["hmac_sig"], sig2)

    # Ledger rows point at the persisted reports.
    assert [row["report_id"] for row in repo.ledger] == [
        b["id"] for b in repo.blame_reports
    ]


def test_ledger_not_written_without_blame_report():
    repo = FakeRepo()
    persist(repo, graph_id=1, blame=None)
    assert repo.ledger == []


def test_breaker_upsert_sets_opened_at_once_per_transition():
    repo = FakeRepo()

    asyncio.run(repo.upsert_breaker("agent_name", "writer", "closed", None))
    assert repo.breakers[("agent_name", "writer")]["opened_at"] is None

    asyncio.run(repo.upsert_breaker("agent_name", "writer", "open", "3 open incidents"))
    opened = repo.breakers[("agent_name", "writer")]["opened_at"]
    assert opened is not None

    # Re-recording an already-open breaker preserves the original stamp.
    asyncio.run(repo.upsert_breaker("agent_name", "writer", "open", "4 open incidents"))
    entry = repo.breakers[("agent_name", "writer")]
    assert entry["opened_at"] == opened
    assert entry["reason"] == "4 open incidents"

    # Close, then reopen: a NEW transition gets a new stamp.
    asyncio.run(repo.upsert_breaker("agent_name", "writer", "closed", "recovered"))
    asyncio.run(repo.upsert_breaker("agent_name", "writer", "open", "tripped again"))
    assert repo.breakers[("agent_name", "writer")]["opened_at"] >= opened

    # One row per scope (UNIQUE upsert), and read_breakers reflects it.
    assert len(repo.breakers) == 1
    breakers = asyncio.run(repo.read_breakers())
    assert len(breakers) == 1
    assert (breakers[0].scope_value, breakers[0].state) == ("writer", "open")


def _seed_incidents(repo: FakeRepo) -> None:
    """Four graphs: three incidents blaming agent 'writer', one blaming 'editor'."""
    repo.add_bundle(
        make_bundle(
            [make_run(1, "writer"), make_run(2, "editor")], [(1, 2)], graph_id=1
        )
    )
    for graph_id, culprit in ((1, 1), (2, 1), (3, 1), (4, 2)):
        if graph_id != 1:
            repo.add_bundle(
                make_bundle([make_run(culprit, "writer" if culprit == 1 else "editor")], [], graph_id=graph_id)
            )
        persist(repo, graph_id=graph_id, blame=make_blame(culprit, {"g": graph_id}))


def test_count_open_incidents_filters_status_and_agent():
    repo = FakeRepo()
    _seed_incidents(repo)

    # All four incidents start open: three blame 'writer', one 'editor'.
    assert asyncio.run(repo.count_open_incidents_for_agent("writer")) == 3
    assert asyncio.run(repo.count_open_incidents_for_agent("editor")) == 1
    assert asyncio.run(repo.count_open_incidents_for_agent("nobody")) == 0

    # acknowledged still counts as live; resolved and superseded do not.
    repo.incidents[(uid(1), "k")]["status"] = "acknowledged"
    repo.incidents[(uid(2), "k")]["status"] = "resolved"
    repo.incidents[(uid(3), "k")]["status"] = "superseded"
    assert asyncio.run(repo.count_open_incidents_for_agent("writer")) == 1


def test_count_open_incidents_uses_only_latest_report():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "writer"), make_run(2, "editor")], [(1, 2)], graph_id=1
        )
    )
    # First analysis blames writer; a re-analysis of the SAME incident blames
    # editor. Only the latest report (is_latest) counts.
    persist(repo, graph_id=1, blame=make_blame(1, {"v": 1}))
    persist(repo, graph_id=1, blame=make_blame(2, {"v": 2}))
    assert asyncio.run(repo.count_open_incidents_for_agent("writer")) == 0
    assert asyncio.run(repo.count_open_incidents_for_agent("editor")) == 1


def test_read_policy_rules_filters_enabled():
    repo = FakeRepo()
    enabled = PolicyRule(
        id=1,
        name="cost-cap",
        predicate={"cost_over": 5.0},
        action="warn",
        shadow=True,
        enabled=True,
    )
    disabled = PolicyRule(
        id=2,
        name="retired",
        predicate={"flags_any": ["failed_runs"]},
        action="block",
        shadow=True,
        enabled=False,
    )
    repo.policy_rules = [enabled, disabled]
    assert asyncio.run(repo.read_policy_rules()) == [enabled]


def test_insert_policy_decisions_records_shadow_mode():
    from worker.types import PolicyDecision

    repo = FakeRepo()
    asyncio.run(
        repo.insert_policy_decisions(
            uid(1),
            [PolicyDecision(rule_name="cost-cap", decision="would_warn", detail="x")],
        )
    )
    assert repo.policy_decisions == [
        {
            "graph_id": uid(1),
            "rule_name": "cost-cap",
            "decision": "would_warn",
            "detail": "x",
            "mode": "shadow",
        }
    ]
