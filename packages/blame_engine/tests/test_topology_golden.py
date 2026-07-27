"""Schema-2 golden layer over the topology archetypes.

test_architectures locks the LEGACY surface (culprits, candidacy prose,
confidence) for the 8 archetypes; detective_ci/examples locks the stable
surface. This file locks what neither does: the TYPED defect layer — origin,
channel, ref polarity, per-origin evidence — on non-linear shapes, plus four
scenarios the archetype battery lacks (diamond shared ancestor, branch
recovery, fan-out verification gap, escalation on a cyclic topology).

Expectations hand-authored BEFORE running (keep-red): a failure is a finding
about the engine, never a reason to bend the assertion.
"""

import pytest

from blame_engine import (
    Design,
    Finding,
    Localized,
    RuleFingerprint,
    TerminalVerdict,
    deserialize_defect,
    find_blame,
)

from test_architectures import (
    BAD_TERMINAL,
    OK_TERMINAL,
    FEEDBACK,
    HIERARCHY,
    MARKET,
    PEER_MESH,
    STAR,
    SWARM,
    _feedback_scores,
)


def _typed(report):
    return report.evidence.findings, [
        deserialize_defect(d) for d in report.evidence.defects
    ]


def _supporting_kinds(findings, defect):
    return {
        findings[r.ref]["kind"] for r in defect.finding_refs if r.role == "supporting"
    }


def _localized_origins(defects, kind):
    return sorted(
        d.origin.run_id
        for d in defects
        if d.kind == kind and isinstance(d.origin, Localized)
    )


# --- archetypes: the typed layer must name the SAME origins ---------------


def test_star_typed_defect_sits_on_the_specialist_not_the_aggregator(mk):
    report = find_blame(
        mk(nodes=STAR["nodes"], edges=STAR["edges"], scores=STAR["scores"],
           terminal_verdict=BAD_TERMINAL)
    )
    findings, defects = _typed(report)
    assert _localized_origins(defects, "content") == ["spec_code"]
    d = next(d for d in defects if d.kind == "content")
    kinds = _supporting_kinds(findings, d)
    assert "content_drop" in kinds
    assert "terminal_content" in kinds
    assert not any(
        isinstance(x.origin, Localized) and x.origin.run_id == "aggregator"
        for x in defects
    )


def test_hierarchy_typed_defect_sits_on_the_deep_worker(mk):
    report = find_blame(
        mk(nodes=HIERARCHY["nodes"], edges=HIERARCHY["edges"],
           scores=HIERARCHY["scores"], terminal_verdict=BAD_TERMINAL)
    )
    findings, defects = _typed(report)
    assert _localized_origins(defects, "content") == ["worker_b1"]
    assert "content_drop" in _supporting_kinds(
        findings, next(d for d in defects if d.kind == "content")
    )


def test_peer_mesh_typed_defect_drills_to_the_worst_scc_member(mk):
    report = find_blame(
        mk(nodes=PEER_MESH["nodes"], edges=PEER_MESH["edges"],
           scores=PEER_MESH["scores"], terminal_verdict=BAD_TERMINAL)
    )
    findings, defects = _typed(report)
    assert _localized_origins(defects, "content") == ["peer_alpha"]
    d = next(d for d in defects if d.kind == "content")
    assert d.channel == "judged"
    assert "content_drop" in _supporting_kinds(findings, d)


def test_swarm_typed_layer_carries_one_defect_per_regional_origin(mk):
    report = find_blame(
        mk(nodes=SWARM["nodes"], edges=SWARM["edges"], scores=SWARM["scores"],
           terminal_verdict=BAD_TERMINAL)
    )
    findings, defects = _typed(report)
    assert report.report_type == "multi_culprit"
    content = [d for d in defects if d.kind == "content"]
    assert _localized_origins(content, "content") == ["drone_03", "drone_09"]
    for d in content:
        drop_refs = [
            r.ref
            for r in d.finding_refs
            if r.role == "supporting" and findings[r.ref]["kind"] == "content_drop"
        ]
        assert len(drop_refs) == 1
        assert findings[drop_refs[0]]["subject"] == f"run:{d.origin.run_id}"


def test_market_typed_defect_names_the_winner_never_the_losers(mk):
    report = find_blame(
        mk(nodes=MARKET["nodes"], edges=MARKET["edges"], scores=MARKET["scores"],
           terminal_verdict=TerminalVerdict(
               bad=True, score=0.2,
               reasoning="the awarded bidder's deliverable is broken",
               checkable=True))
    )
    _findings, defects = _typed(report)
    assert _localized_origins(defects, "content") == ["bidder_b"]
    for d in defects:
        assert not (
            isinstance(d.origin, Localized)
            and d.origin.run_id in ("bidder_a", "bidder_c")
        )


# --- feedback loop: deterministic channel + escalation on a CYCLE ---------


def test_feedback_contract_defect_is_deterministic_and_unverified(mk):
    """Cyclic topology (eval->act loopback): the contract defect localizes at
    think through the SCC, deterministic, with the honest caveat — nothing
    verified the breach's fate in the shipped artifact."""
    report = find_blame(
        mk(nodes=FEEDBACK["nodes"], edges=FEEDBACK["edges"],
           scores=_feedback_scores(), terminal_verdict=OK_TERMINAL)
    )
    findings, defects = _typed(report)
    assert report.report_type == "degraded_recovered"
    contract = [d for d in defects if d.kind == "contract"]
    assert len(contract) == 1
    d = contract[0]
    assert d.channel == "deterministic"
    assert d.origin == Localized(run_id="think")
    assert d.unverified_in_channel == "content"
    assert d.propagation == ()
    assert "contract_breach" in _supporting_kinds(findings, d)
    # No defect blames the loopback SCC members.
    for x in defects:
        assert not (
            isinstance(x.origin, Localized)
            and x.origin.run_id in ("act", "render", "qa", "eval")
        )


_PROPAGATED = Finding(
    kind="breach_propagated",
    channel="deterministic",
    subject="terminal",
    data={
        "key": "file_type",
        "from": "docx",
        "to": "md",
        "basis": "artifact path 'out/report.md' ends '.md'",
        "deliverable_run_id": "render",
    },
    provenance=RuleFingerprint(
        rule="contract_propagation:file_type",
        detail="artifact path 'out/report.md' ends '.md'",
    ),
    certainty=1.0,
)


def test_feedback_escalates_with_verified_propagation_on_the_cycle(mk):
    """Same cyclic topology + a worker-verified propagation: the engine
    escalates in the single pass, the caveat is gone, the propagation is in
    the refs — topology must not change the escalation contract."""
    report = find_blame(
        mk(nodes=FEEDBACK["nodes"], edges=FEEDBACK["edges"],
           scores=_feedback_scores(), terminal_verdict=OK_TERMINAL),
        extra_findings=[_PROPAGATED],
    )
    findings, defects = _typed(report)
    assert report.report_type == "shipped_with_latent_defect"
    d = next(x for x in defects if x.kind == "contract")
    assert "breach_propagated" in _supporting_kinds(findings, d)
    assert d.propagation == ("render",)
    assert d.unverified_in_channel is None


# --- scenarios the archetype battery lacks --------------------------------

_DIAMOND_NODES = ["start", "orch", "planner", "w1", "w2", "join"]
_DIAMOND_EDGES = [
    ("start", "orch"),
    ("orch", "planner"),
    ("planner", "w1"),
    ("planner", "w2"),
    ("w1", "join"),
    ("w2", "join"),
]


def test_diamond_shared_ancestor_is_one_origin_not_multi_culprit(mk):
    """Both branches degraded because their SHARED ancestor poisoned them: one
    cause, one defect at the ancestor — never multi_culprit smeared over the
    branches, never a defect on the join."""
    report = find_blame(
        mk(nodes=_DIAMOND_NODES, edges=_DIAMOND_EDGES,
           scores={"start": None, "orch": 0.9, "planner": 0.2,
                   "w1": 0.3, "w2": 0.35, "join": 0.3},
           terminal_verdict=BAD_TERMINAL)
    )
    findings, defects = _typed(report)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["planner"]
    assert _localized_origins(defects, "content") == ["planner"]
    for x in defects:
        assert not (
            isinstance(x.origin, Localized)
            and x.origin.run_id in ("w1", "w2", "join")
        )
    assert "content_drop" in _supporting_kinds(
        findings, next(d for d in defects if d.kind == "content")
    )


_FANOUT_NODES = ["start", "orch", "w1", "w2", "w3", "join"]
_FANOUT_EDGES = [
    ("start", "orch"),
    ("orch", "w1"), ("orch", "w2"), ("orch", "w3"),
    ("w1", "join"), ("w2", "join"), ("w3", "join"),
]


def test_fanout_branch_dip_that_recovers_is_a_recovered_near_miss(mk):
    """w2 dips, the join and terminal recover: a near-miss on a BRANCH — a
    recovered content defect at w2 (visible, never a live break), and the
    projection derives degraded_recovered, not cut_point."""
    report = find_blame(
        mk(nodes=_FANOUT_NODES, edges=_FANOUT_EDGES,
           scores={"start": None, "orch": 0.9, "w1": 0.9, "w2": 0.2,
                   "w3": 0.9, "join": 0.9},
           terminal_verdict=OK_TERMINAL)
    )
    findings, defects = _typed(report)
    assert report.report_type == "degraded_recovered"
    content = [d for d in defects if d.kind == "content"]
    assert len(content) == 1
    d = content[0]
    assert d.origin == Localized(run_id="w2")
    assert d.recovered is True
    assert "content_drop" in _supporting_kinds(findings, d)
    # The ok terminal is part of the recovered STORY — context, not refutation.
    tc_ref = next(
        r for r in d.finding_refs
        if findings[r.ref]["kind"] == "terminal_content"
    )
    assert tc_ref.role == "context"


def test_fanout_healthy_producers_bad_terminal_is_a_verifier_gap(mk):
    """Every producer and the join scored healthy, the verifier passed, yet
    the terminal is bad: the verifier that let it through IS the failure —
    a verification defect localized at qa, supported by its verdict finding."""
    nodes = ["start", "orch", "w1", "w2", "w3", "join", "qa"]
    edges = _FANOUT_EDGES + [("join", "qa")]
    report = find_blame(
        mk(nodes=nodes, edges=edges,
           scores={"start": None, "orch": 0.9, "w1": 0.9, "w2": 0.9,
                   "w3": 0.9, "join": 0.9, "qa": 1.0},
           terminal_verdict=BAD_TERMINAL)
    )
    findings, defects = _typed(report)
    assert report.report_type == "verification_gap"
    verification = [d for d in defects if d.kind == "verification"]
    assert _localized_origins(verification, "verification") == ["qa"]
    d = verification[0]
    assert d.channel == "judged"
    assert _supporting_kinds(findings, d) == {"verifier_verdict"}
