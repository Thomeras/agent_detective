"""Architecture regression battery: 8 multi-agent archetypes, each with an
injected fault, locking WHERE the engine localises blame and how the topology
classifier (frozen contract, blame_engine.topology) describes the shape.

The archetypes mirror the fixtures in packages/detective_ci/examples/arch_*.json
(golden-locked there on the stable surface); this file locks the RICHER engine
surface: confidences and their caps, candidacy wording, loop-drill semantics,
shadowing, and the advisory topology classification.

Topology is ADVISORY: it must never change report_type, confidence, culprits or
candidacy — every archetype test here locks the verdict itself, so any
behavioral leak from the classifier fails this battery.
"""

import pytest

from blame_engine import NodeScore, TerminalVerdict, find_blame
from conftest import note_of, verdict_of

try:  # agent T's classifier — frozen contract; skip topology locks until landed
    from blame_engine.topology import classify_topology
except ImportError:  # pragma: no cover
    classify_topology = None

requires_topology = pytest.mark.skipif(
    classify_topology is None,
    reason="blame_engine.topology not landed yet (parallel agent T)",
)

BAD_TERMINAL = TerminalVerdict(
    bad=True, score=0.3, reasoning="final deliverable is broken", checkable=True
)
OK_TERMINAL = TerminalVerdict(
    bad=False, score=1.0, reasoning="deliverable aligns with the request", checkable=True
)


def _ns(run_id: str, score: float, **kw) -> NodeScore:
    return NodeScore(
        run_id=run_id,
        score=score,
        components=kw.pop("components", {}),
        input_flawed=kw.pop("input_flawed", False),
        unscored_reason=None,
        judge_note=kw.pop("judge_note", None),
        **kw,
    )


# ---------------------------------------------------------------------------
# Archetype graph definitions (shared by the engine tests and topology locks).
# These mirror packages/detective_ci/examples/arch_*.json byte-for-byte in
# structure; the goldens there lock the stable surface, this file the rest.
# ---------------------------------------------------------------------------

SINGLE_AGENT = {
    "nodes": ["solo"],
    "edges": [],
    "scores": {"solo": 0.1},
}

STAR = {
    "nodes": ["orchestrator", "spec_research", "spec_code", "spec_data",
              "spec_writer", "aggregator"],
    "edges": [
        ("orchestrator", "spec_research"),
        ("orchestrator", "spec_code"),
        ("orchestrator", "spec_data"),
        ("orchestrator", "spec_writer"),
        ("spec_research", "aggregator"),
        ("spec_code", "aggregator"),
        ("spec_data", "aggregator"),
        ("spec_writer", "aggregator"),
    ],
    "scores": {
        "orchestrator": 0.95,
        "spec_research": 0.9,
        "spec_code": 0.2,       # injected fault: one specialist fails
        "spec_data": 0.88,
        "spec_writer": 0.9,
        "aggregator": 0.35,     # inherits the broken section
    },
}

HIERARCHY = {
    "nodes": ["manager", "lead_alpha", "lead_beta", "worker_a1", "worker_a2",
              "worker_b1", "worker_b2", "merge"],
    "edges": [
        ("manager", "lead_alpha"),
        ("manager", "lead_beta"),
        ("lead_alpha", "worker_a1"),
        ("lead_alpha", "worker_a2"),
        ("lead_beta", "worker_b1"),
        ("lead_beta", "worker_b2"),
        ("worker_a1", "merge"),
        ("worker_a2", "merge"),
        ("worker_b1", "merge"),
        ("worker_b2", "merge"),
    ],
    "scores": {
        "manager": 0.95,
        "lead_alpha": 0.92,
        "lead_beta": 0.9,
        "worker_a1": 0.88,
        "worker_a2": 0.9,
        "worker_b1": 0.15,      # injected fault: deep worker fabricates
        "worker_b2": 0.87,
        "merge": 0.3,           # inherits the fabricated section
    },
}

PEER_MESH = {
    "nodes": ["peer_alpha", "peer_bravo", "peer_charlie", "peer_delta"],
    "edges": [
        ("peer_alpha", "peer_bravo"), ("peer_bravo", "peer_alpha"),
        ("peer_bravo", "peer_charlie"), ("peer_charlie", "peer_bravo"),
        ("peer_charlie", "peer_delta"), ("peer_delta", "peer_charlie"),
        ("peer_delta", "peer_alpha"), ("peer_alpha", "peer_delta"),
    ],
    "scores": {
        "peer_alpha": 0.15,     # injected fault: poisons the mesh
        "peer_bravo": 0.4,
        "peer_charlie": 0.45,
        "peer_delta": 0.4,      # exit node (latest end_time): 0.4 < threshold
    },
}

PIPELINE = {
    "nodes": ["ingest", "plan", "draft", "refine", "publish"],
    "edges": [("ingest", "plan"), ("plan", "draft"), ("draft", "refine"),
              ("refine", "publish")],
    "scores": {
        "ingest": 0.95,
        "plan": 0.9,
        "draft": 0.2,           # injected fault: mid-stage drops sections
        "refine": 0.35,
        "publish": 0.3,
    },
}

FEEDBACK = {
    "nodes": ["start", "think", "act", "render", "qa", "eval"],
    "edges": [("start", "think"), ("think", "act"), ("act", "render"),
              ("render", "qa"), ("qa", "eval"), ("eval", "act")],
    # scores built in the test (think carries flags + a contract violation,
    # start is a payload-less structural root).
}

MARKET = {
    "nodes": ["orchestrator", "bidder_a", "bidder_b", "bidder_c"],
    "edges": [("orchestrator", "bidder_a"), ("orchestrator", "bidder_b"),
              ("orchestrator", "bidder_c")],
    "scores": {
        "orchestrator": 0.95,
        "bidder_a": 0.9,        # loser: healthy leaf sink
        "bidder_b": 0.15,       # injected fault: awarded winner executes badly
        "bidder_c": 0.88,       # loser: healthy leaf sink
    },
}

SWARM = {
    "nodes": [f"drone_{i:02d}" for i in range(1, 13)],
    # Dense but FIXED edge set: two weakly-linked regions (01-06, 07-12).
    "edges": [
        ("drone_01", "drone_02"), ("drone_01", "drone_03"),
        ("drone_02", "drone_03"), ("drone_02", "drone_04"),
        ("drone_03", "drone_04"), ("drone_03", "drone_05"),
        ("drone_04", "drone_05"), ("drone_04", "drone_06"),
        ("drone_05", "drone_06"),
        ("drone_07", "drone_08"), ("drone_07", "drone_09"),
        ("drone_08", "drone_09"), ("drone_08", "drone_10"),
        ("drone_09", "drone_10"), ("drone_09", "drone_11"),
        ("drone_10", "drone_11"), ("drone_10", "drone_12"),
        ("drone_11", "drone_12"),
        ("drone_02", "drone_08"), ("drone_06", "drone_12"),
    ],
    "scores": {
        "drone_01": 0.95, "drone_02": 0.92,
        "drone_03": 0.18,   # injected regional failure #1
        "drone_04": 0.35, "drone_05": 0.3, "drone_06": 0.33,
        "drone_07": 0.93, "drone_08": 0.9,
        "drone_09": 0.2,    # injected regional failure #2 (independent of #1)
        "drone_10": 0.34, "drone_11": 0.31, "drone_12": 0.28,
    },
}


def _feedback_scores() -> dict:
    return {
        # "start" intentionally omitted -> unscored structural root
        "think": _ns(
            "think", 0.15,
            flags=("missing_required_content",),
            contract_violations=(("file_type", "docx", "md"),),
            judge_note="only an outline; silently switched docx to md",
        ),
        "act": 0.93,
        "render": 0.93,
        "qa": 1.0,
        "eval": 1.0,
    }


# ---------------------------------------------------------------------------
# 1. single_agent — the fault is the node itself; observability-boundary cap.
# ---------------------------------------------------------------------------

def test_single_agent_blames_itself_with_boundary_capped_attribution(mk) -> None:
    inp = mk(nodes=SINGLE_AGENT["nodes"], edges=SINGLE_AGENT["edges"],
             scores=SINGLE_AGENT["scores"], terminal_verdict=BAD_TERMINAL)
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["solo"]
    assert report.propagation_path == ["solo"]

    # The cap IS the lock: a single node has no scored predecessor, so its 1.00
    # baseline is assumed — attribution (and the headline confidence, which for
    # cut_point is attribution) can never exceed the 0.6 observability-boundary
    # ceiling, no matter how bad the output (raw formula here would give 0.94).
    assert report.confidence == pytest.approx(0.6)
    assert report.evidence.attribution_confidence == pytest.approx(0.6)
    # ... while the OBSERVATION (is the output defective?) is not capped:
    # severity (0.5 - 0.1) / 0.5 = 0.8.
    assert report.evidence.observation_confidence == pytest.approx(0.8)
    assert note_of(report, "attribution_capped") is not None
    content = next(
        b for b in report.evidence.attribution_breakdown
        if b["defect"] == "content_degradation"
    )
    assert content["attribution"] == pytest.approx(0.6)
    assert "observability boundary" in content["basis"]
    assert verdict_of(report, "solo").startswith("origin")


# ---------------------------------------------------------------------------
# 2. star — one specialist fails; the aggregator inherits, never blamed.
# ---------------------------------------------------------------------------

def test_star_blames_failing_specialist_aggregator_inherits(mk) -> None:
    inp = mk(nodes=STAR["nodes"], edges=STAR["edges"], scores=STAR["scores"],
             terminal_verdict=BAD_TERMINAL)
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["spec_code"]
    assert report.propagation_path == ["spec_code", "aggregator"]
    # The aggregator dropped 0.55 from a healthy spoke — origin-shaped — but is
    # SHADOWED by its ancestor origin: inherited degradation, not a second cause.
    assert verdict_of(report, "aggregator") == "inherited"
    assert report.evidence.drops["spec_code"] == pytest.approx(0.75)
    assert report.evidence.drops["aggregator"] == pytest.approx(0.55)
    for healthy in ("orchestrator", "spec_research", "spec_data", "spec_writer"):
        assert verdict_of(report, healthy) == "healthy"


# ---------------------------------------------------------------------------
# 3. hierarchy — a deep worker fails three levels down; merge inherits.
# ---------------------------------------------------------------------------

def test_hierarchy_blames_deep_worker_merge_inherits(mk) -> None:
    inp = mk(nodes=HIERARCHY["nodes"], edges=HIERARCHY["edges"],
             scores=HIERARCHY["scores"], terminal_verdict=BAD_TERMINAL)
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["worker_b1"]
    assert report.propagation_path == ["worker_b1", "merge"]
    assert verdict_of(report, "merge") == "inherited"
    for healthy in ("manager", "lead_alpha", "lead_beta", "worker_a1",
                    "worker_a2", "worker_b2"):
        assert verdict_of(report, healthy) == "healthy"


# ---------------------------------------------------------------------------
# 4. peer_mesh — one peer poisons a fully bidirectional 4-SCC. LOCKED REALITY:
# the whole mesh condenses to ONE super-node whose score is the EXIT member's
# (latest end_time); being degraded it becomes the single candidate, and
# _drill_into_loop then reassigns blame to the WORST-SCORING MEMBER, with the
# drop recomputed against that member's raw in-mesh predecessors.
# ---------------------------------------------------------------------------

def test_peer_mesh_drills_into_scc_and_blames_worst_member(mk) -> None:
    inp = mk(nodes=PEER_MESH["nodes"], edges=PEER_MESH["edges"],
             scores=PEER_MESH["scores"], terminal_verdict=BAD_TERMINAL)
    report = find_blame(inp)

    # Blame lands INSIDE the SCC, on the worst member — not on the exit node
    # (peer_delta) that merely carried the poisoned consensus downstream.
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["peer_alpha"]
    # "cycle", not "retry loop": the engine has no edge types, and the shape is
    # just as often an orchestrator↔sub-agent delegation pair.
    cut = note_of(report, "cut_point")
    assert cut["variant"] == "loop" and cut["members"] == 4
    # Names the exit that only carried it, and says the drill MOVED the blame.
    assert cut["exit_run_id"] == "peer_delta" and cut["drilled"] is True
    # The drilled drop is measured against the member's raw in-mesh
    # predecessors (bravo 0.40 / delta 0.40 -> alpha 0.15), not the exit drop.
    assert report.evidence.drops["peer_alpha"] == pytest.approx(0.25)
    for member in ("peer_bravo", "peer_charlie", "peer_delta"):
        assert verdict_of(report, member) == "loop_member"
        assert member not in report.culprit_run_ids
    # A 4-member SCC is under max_loop_iterations (10) with no baselines: this
    # is mesh collaboration, NOT an anomalous loop.
    assert report.evidence.loop_anomalies == []
    # Multi-member SCC honesty: the scc penalty (x0.8) applies to attribution.
    # (0.5*gap(0.25/0.5) + 0.3*sev(0.7) + 0.2*pred(0.0)) * 0.8 = 0.368.
    # The predecessor term is 0 because alpha's real in-mesh predecessors are
    # 0.40 — below threshold. It read 1.0 (=> 0.528) while the drop came from
    # those same 0.40 predecessors but the baseline came from the SCC's ASSUMED
    # 1.00 source baseline: the gap term measured the mesh, the predecessor term
    # measured a fiction. In a mesh where everyone is already degraded,
    # attribution of the break to one peer IS weaker, and the number now says so.
    assert report.confidence == pytest.approx(0.368)
    # All four members expanded on the propagation path, end-time order.
    assert report.propagation_path == PEER_MESH["nodes"]


# ---------------------------------------------------------------------------
# 5. pipeline — mid-stage fails, everything downstream inherits/shadowed.
# ---------------------------------------------------------------------------

def test_pipeline_blames_mid_stage_downstream_inherited(mk) -> None:
    inp = mk(nodes=PIPELINE["nodes"], edges=PIPELINE["edges"],
             scores=PIPELINE["scores"], terminal_verdict=BAD_TERMINAL)
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["draft"]
    assert report.propagation_path == ["draft", "refine", "publish"]
    assert report.evidence.drops["draft"] == pytest.approx(0.7)
    for inherited in ("refine", "publish"):
        assert verdict_of(report, inherited) == "inherited"
    assert verdict_of(report, "ingest") == "healthy"
    assert verdict_of(report, "plan") == "healthy"


# ---------------------------------------------------------------------------
# 6. graph_with_feedback — LangGraph-style loopback (eval->act) + a contract
# violation at think, ok terminal -> engine-level degraded_recovered (the
# escalatable surface WITHOUT worker escalation).
# ---------------------------------------------------------------------------

def test_feedback_loop_contract_violation_is_degraded_recovered(mk) -> None:
    inp = mk(nodes=FEEDBACK["nodes"], edges=FEEDBACK["edges"],
             scores=_feedback_scores(), terminal_verdict=OK_TERMINAL)
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["think"]
    # The act/render/qa/eval loopback SCC condensed and scored by its exit
    # (eval, 1.0): healthy successors + ok terminal = recovery, so the SCC is
    # never blamed for think's breach.
    assert not set(report.culprit_run_ids) & {"act", "render", "qa", "eval"}
    # Split confidence: the deterministic contract breach pins the OBSERVATION
    # near-certain (0.95, the headline for degraded_recovered). Attribution is
    # the verdict-carrying defect's — the contract violation, whose origination
    # is observed, so no boundary ceiling applies.
    assert report.confidence == pytest.approx(0.95)
    assert report.evidence.observation_confidence == pytest.approx(0.95)
    assert report.evidence.attribution_confidence == pytest.approx(0.95)
    contract = next(
        b for b in report.evidence.attribution_breakdown
        if b["defect"] == "contract_violation"
    )
    assert contract["attribution"] == pytest.approx(0.95)
    # The ok terminal must NOT clear the breach: recovered in content,
    # unverified in contract — said out loud.
    assert note_of(report, "contract_vs_terminal") is not None
    assert report.evidence.contract_violations == [
        {"run_id": "think", "agent": "think", "key": "file_type",
         "from": "docx", "to": "md"}
    ]
    assert report.evidence.verification_gaps == []
    assert verdict_of(report, "start") == "structural_root"


# ---------------------------------------------------------------------------
# 7. market — orchestrator fans a bid out, the awarded winner executes badly.
# Losers are healthy leaf sinks and must never be blamed.
# ---------------------------------------------------------------------------

def test_market_blames_winner_never_the_losing_bidders(mk) -> None:
    inp = mk(nodes=MARKET["nodes"], edges=MARKET["edges"],
             scores=MARKET["scores"],
             terminal_verdict=TerminalVerdict(
                 bad=True, score=0.2,
                 reasoning="the awarded bidder's deliverable is broken",
                 checkable=True))
    report = find_blame(inp)

    assert report.report_type == "cut_point"          # NOT multi_culprit
    assert report.culprit_run_ids == ["bidder_b"]
    assert report.propagation_path == ["bidder_b"]
    assert report.evidence.drops["bidder_b"] == pytest.approx(0.8)
    # Healthy losers: never candidates, never culprits, never verification gaps.
    for loser in ("bidder_a", "bidder_c"):
        assert loser not in report.culprit_run_ids
    assert report.evidence.verification_gaps == []
    # LOCKED REALITY: with a bad terminal, ALL non-verifier sinks land in
    # manifestation, so the healthy loser sinks currently pick up a
    # claims-vs-reality label (their healthy score is demoted to a claim, not
    # blame). Blame localisation itself is untouched — the assertion above is
    # the contract; this one documents today's side-band evidence.
    for loser in ("bidder_a", "bidder_c"):
        assert verdict_of(report, loser) == "claims_conflict"


# ---------------------------------------------------------------------------
# 8. swarm — 12 densely-wired drones, two INDEPENDENT regional failures.
# ---------------------------------------------------------------------------

def test_swarm_two_independent_regional_failures_multi_culprit(mk) -> None:
    inp = mk(nodes=SWARM["nodes"], edges=SWARM["edges"], scores=SWARM["scores"],
             terminal_verdict=BAD_TERMINAL)
    report = find_blame(inp)

    assert report.report_type == "multi_culprit"
    # Exactly the two injected regional origins — neither shadows the other
    # (neither is the other's ancestor), and every dense degraded neighbour is
    # shadowed by its own regional origin.
    assert report.culprit_run_ids == ["drone_03", "drone_09"]
    for inherited in ("drone_04", "drone_05", "drone_06",
                      "drone_10", "drone_11", "drone_12"):
        assert verdict_of(report, inherited) == "inherited"
    for healthy in ("drone_01", "drone_02", "drone_07", "drone_08"):
        assert verdict_of(report, healthy) == "healthy"
    # Honest multi-culprit ceiling: penalised per candidate, capped at 0.8.
    assert 0.5 < report.confidence <= 0.8


# ---------------------------------------------------------------------------
# Topology locks (frozen contract, agent T's classifier). Advisory only: the
# verdict assertions above are the guard that classification never changes
# blame behavior.
# ---------------------------------------------------------------------------

TOPOLOGY_EXPECTED = [
    ("single_agent", SINGLE_AGENT["nodes"], SINGLE_AGENT["edges"], "single_node"),
    ("star", STAR["nodes"], STAR["edges"], "star"),
    # Frozen contract reality: the merge/aggregator sink gives four nodes
    # in-degree > 1, so the manager->leads->workers->merge shape is NOT a tree
    # and classifies "dag" (the tree-only "hierarchy" class is locked below).
    ("hierarchy_with_merge", HIERARCHY["nodes"], HIERARCHY["edges"], "dag"),
    ("peer_mesh", PEER_MESH["nodes"], PEER_MESH["edges"], "mesh"),
    ("pipeline", PIPELINE["nodes"], PIPELINE["edges"], "pipeline"),
    ("graph_with_feedback", FEEDBACK["nodes"], FEEDBACK["edges"],
     "pipeline_with_feedback"),
    # Market = pure fan-out: every bidder 1 hop from the single root -> star.
    ("market", MARKET["nodes"], MARKET["edges"], "star"),
    ("swarm", SWARM["nodes"], SWARM["edges"], "dag"),
]


@requires_topology
@pytest.mark.parametrize(
    "name,nodes,edges,expected",
    TOPOLOGY_EXPECTED,
    ids=[t[0] for t in TOPOLOGY_EXPECTED],
)
def test_topology_primary_per_archetype(name, nodes, edges, expected) -> None:
    result = classify_topology(list(nodes), list(edges))
    assert result["primary"] == expected
    assert result["components"] == 1  # every archetype here is one connected run


@requires_topology
def test_topology_hierarchy_class_needs_a_true_tree() -> None:
    """The manager->leads->workers TREE (no merge sink) IS the 'hierarchy'
    class: single root, every non-root in-degree 1, depth >= 3."""
    nodes = [n for n in HIERARCHY["nodes"] if n != "merge"]
    edges = [(u, v) for u, v in HIERARCHY["edges"] if v != "merge"]
    assert classify_topology(nodes, edges)["primary"] == "hierarchy"


@requires_topology
def test_topology_mesh_attributes() -> None:
    result = classify_topology(list(PEER_MESH["nodes"]), list(PEER_MESH["edges"]))
    assert result["scc_count"] == 1            # one nontrivial SCC: the mesh
    assert result["bidirectional_pairs"] == 4  # A<->B, B<->C, C<->D, D<->A


@requires_topology
@pytest.mark.parametrize(
    "name,nodes,edges,expected",
    TOPOLOGY_EXPECTED,
    ids=[t[0] for t in TOPOLOGY_EXPECTED],
)
def test_evidence_topology_matches_contract_when_attached(
    mk, name, nodes, edges, expected
) -> None:
    """Once the worker/engine attaches evidence.topology, its primary must be
    the frozen-contract class. Tolerant of the attachment not existing yet."""
    scores = {
        n: 0.9 for n in nodes
    }
    inp = mk(nodes=nodes, edges=edges, scores=scores)
    topo = getattr(find_blame(inp).evidence, "topology", None)
    if topo is None:
        pytest.skip("evidence.topology not attached by the engine (yet)")
    assert isinstance(topo, dict)
    assert topo.get("primary") == expected
