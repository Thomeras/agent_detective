"""The non-linear topology corpus: does graph-first localization actually hold?

``docs/capabilities.md`` admits under "Honest limits" that the corpus is "ONE
harness, ONE linear topology, ONE injection — the graph-first thesis (fan-out,
retry loops, A2A) awaits its validating trace". The lattice already has unit
coverage of these shapes (``test_fanout_golden``, ``test_loops``,
``test_cycle_localization``), but every one of those hand-writes the edge list.
That is precisely the assumption under test: the MAPPER decides what graph the
engine ever sees, so a lattice that localizes perfectly on a hand-drawn fan-out
proves nothing about a fan-out that arrived as OTLP spans.

The corpus closes that gap. ``scripts/gen_topology_traces.py`` emits SIX
OTLP/HTTP JSON traces under ``testdata/topologies/`` and records the graph the
REAL mapper reconstructs into ``fixtures/topology_corpus.json``. Nodes are keyed
by AGENT NAME (unique per cell) rather than the derived run-id uuid5, so the
ground truth is readable.

FOUR cells carry an injected contract fault; ``retry_loop_recovered`` and
``retry_runaway`` carry NONE (they are negative controls — the runaway's breach
is purely structural). "Five cells, one fault each" was the original summary and
it was wrong about two of them; ``INJECTED_CULPRIT`` below is the inventory, and
``test_the_injected_culprit_table_is_checked_against_the_traces`` verifies it
against the payloads instead of asserting it against itself.

Two layers are validated here, and they are NOT the same claim:

**Layer A — reconstruction** (needs the mapper; skipped when it is not
installed). The OTLP payload really does become the intended shape: fan-out with
a fan-in join, a strongly-connected retry cycle, an A2A diamond. Without this,
every localization result below would be a statement about a graph nobody
proved exists.

**Layer B — localization over a reconstructed graph GIVEN scores** (pure
``blame_engine``). ``find_blame`` over the RECONSTRUCTED graph with per-node
quality scores THIS FILE SUPPLIES. Read what that does and does not establish:
the score vector is authored, the injected `format` rewrite contributes nothing
to it, and no judge ran — so layer B is not a fault→blame measurement end to
end. What it adds over the pre-existing hand-drawn fixtures is the EDGE LIST: it
shows the lattice keeps discriminating origin from inherited on edges a real
OTLP payload produced. ``test_graph_first_beats_node_isolation`` states that
difference as a measurable one, and it is a claim about reading a score vector
as a graph, nothing more.

**The deterministic end-to-end path** (no judge anywhere, scores all None) is
the one place the corpus DOES go fault→verdict: the injected rewrite is detected
by the real contract check, localized by the lattice and projected into a
report. That path used to die in ``find_blame``'s first cascade row (all scores
None ⇒ "no_scores" ⇒ unclassified, ahead of the candidate list); the fix and its
regression pins are in the end-to-end section below.

Expectations were written before running (keep-red discipline, as in
test_fanout_golden): a failure here is a finding about the engine, never a
reason to bend the assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blame_engine import (
    BlameConfig,
    BlameInput,
    NodeScore,
    TerminalVerdict,
    derive_incident,
    find_blame,
)
from blame_engine.condense import condense
from blame_engine.cutpoint import select_candidates
from blame_engine.topology import classify_topology

_FIXTURE = Path(__file__).parent / "fixtures" / "topology_corpus.json"
_TRACES = Path(__file__).resolve().parents[3] / "testdata" / "topologies"

CORPUS: dict = json.loads(_FIXTURE.read_text(encoding="utf-8"))

# Ground truth: which agent INVENTED the nonconformant value. `None` means the
# cell carries no content/contract fault at all. Checked against the trace
# payloads by test_the_injected_culprit_table_is_checked_against_the_traces —
# this table is documentation, and documentation that nothing verifies drifts.
INJECTED_CULPRIT = {
    "fanout_branch_fault": "write_specs",
    # Same injection; the joiner ECHOES the rewritten value it was handed, so the
    # payload shows TWO nodes with a format diff and only one invented it.
    "fanout_join_echoes_breach": "write_specs",
    "retry_loop_unrecovered": "revise",
    "retry_loop_recovered": None,
    "retry_runaway": None,          # structural only: iterations past the limit
    "a2a_diamond_fault": "translator",
}

# The contract key every injection rewrites. Named once: the ground-truth reader
# below parses the raw OTLP payload and must look for the same thing the
# deterministic check does.
_CONTRACT_KEY = "format"


def _scalar_values(payload: object, key: str) -> list[object]:
    """Every DISTINCT scalar value ``key`` takes anywhere in a decoded payload."""
    out: list[object] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key and not isinstance(v, (dict, list)) and v not in out:
                    out.append(v)
                walk(v)
        elif isinstance(node, list):
            for element in node:
                walk(element)

    walk(payload)
    return out


def agents_with_a_contract_diff(cell: str) -> dict[str, tuple[object, object]]:
    """Agents whose OWN span shows ``format`` arriving as one value and leaving
    as another — ground truth read from the TRACE FILE.

    Deliberately independent of both the engine and ``worker.scoring``: it parses
    the OTLP payload directly, so "the fault is really in the data" is not
    asserted from the same code that is supposed to find it. A node whose input
    carries the key ambiguously (a fan-in handed two different values) is not a
    diff — there is no single arriving value to have changed.
    """
    payload = json.loads((_TRACES / CORPUS[cell]["file"]).read_text(encoding="utf-8"))
    diffs: dict[str, tuple[object, object]] = {}
    for resource in payload.get("resourceSpans", []):
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                attrs = {
                    a["key"]: next(iter(a["value"].values()))
                    for a in span.get("attributes", [])
                }
                if attrs.get("openinference.span.kind") != "AGENT":
                    continue
                try:
                    arrived = _scalar_values(
                        json.loads(attrs.get("input.value") or "null"), _CONTRACT_KEY
                    )
                    left = _scalar_values(
                        json.loads(attrs.get("output.value") or "null"), _CONTRACT_KEY
                    )
                except ValueError:  # not JSON: nothing to compare
                    continue
                if len(arrived) == 1 and len(left) == 1 and arrived[0] != left[0]:
                    diffs[attrs["gen_ai.agent.name"]] = (arrived[0], left[0])
    return diffs


_BAD_TERMINAL = TerminalVerdict(
    bad=True,
    score=0.15,
    reasoning="the deliverable mixes markdown and raw HTML",
    checkable=True,
)
_OK_TERMINAL = TerminalVerdict(
    bad=False, score=0.9, reasoning="the deliverable reads correctly", checkable=True
)


def _node_score(run_id: str, value: float | None) -> NodeScore:
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=None,
        # A payload-less orchestrator wrapper is `payload_missing` (a structural
        # root), which is what the real pipeline records for it — using the
        # generic unknown here would turn it into a confidence-capping blind spot.
        unscored_reason="payload_missing" if value is None else None,
        judge_note=None,
    )


def blame_input(
    cell: str,
    scores: dict[str, float | None],
    terminal: TerminalVerdict | None = None,
    *,
    config: BlameConfig | None = None,
) -> BlameInput:
    """A BlameInput over the graph the MAPPER reconstructed for ``cell``.

    Only ``scores`` are synthetic. Nodes, edges and end times come from the
    recorded reconstruction, so a localization result here is a statement about
    a graph a real OTLP payload produced.
    """
    meta = CORPUS[cell]
    nodes = list(meta["nodes"])
    edges = [(u, v) for u, v, _type in meta["edges"]]
    end_times = meta["end_times"]
    missing = set(scores) - set(nodes)
    assert not missing, f"{cell}: no such node(s) {sorted(missing)}"
    return BlameInput(
        nodes=nodes,
        edges=edges,
        scores={n: _node_score(n, scores.get(n)) for n in nodes},
        node_costs={n: 1.0 for n in nodes},
        node_end_times={n: float(end_times.get(n, 0.0)) for n in nodes},
        agent_names={n: n for n in nodes},
        error_span_ids={n: [] for n in nodes},
        terminal_verdict=terminal,
        loop_baselines={},
        config=config or BlameConfig(),
    )


def verdict_for(report, run_id: str) -> str:
    return report.evidence.candidacy_records[run_id]["verdict"]


def run_id_of(names: dict[str, str], agent: str) -> str:
    """The run_id the mapper derived for ``agent`` (uuid5 of trace:span).

    Only needed in the end-to-end tests: layer B keys nodes by agent name, but a
    report built from the real bundle carries derived uuids."""
    return next(run_id for run_id, name in names.items() if name == agent)


# ---------------------------------------------------------------------------
# Layer A — the OTLP payload really reconstructs into the intended shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell", sorted(CORPUS))
def test_recorded_reconstruction_still_matches_the_live_mapper(cell):
    """The fixture is a RECORDING of what the mapper builds from the trace file.

    Re-derive it and compare. Without this the layer-B results would drift the
    moment an edge rule changed: the engine would keep localizing perfectly on a
    graph the pipeline no longer produces. Skipped (not failed) where the mapper
    is not installed — the blame-engine package suite runs alone.
    """
    pytest.importorskip("otel_mapper")
    bundle_mod = pytest.importorskip("detective_cli.bundle")

    meta = CORPUS[cell]
    exports = bundle_mod.load_trace(_TRACES / meta["file"])
    bundles = bundle_mod.bundles_from_exports(exports, a2a_detection=meta["a2a"])
    assert len(bundles) == 1, f"{cell}: expected one graph, got {len(bundles)}"
    bundle = bundles[0]

    names = {str(r.run_id): (r.agent_name or str(r.run_id)) for r in bundle.runs}
    assert [names[str(r.run_id)] for r in bundle.runs] == meta["nodes"]
    assert sorted(
        [names[str(e.from_run_id)], names[str(e.to_run_id)], e.type]
        for e in bundle.edges
    ) == meta["edges"]


def test_corpus_covers_the_three_shapes_the_thesis_was_built_for():
    """Fan-out (both joiner payloads), retry loop (both outcomes), runaway loop
    and A2A — the shapes docs/capabilities.md lists as unproven."""
    assert sorted(CORPUS) == [
        "a2a_diamond_fault",
        "fanout_branch_fault",
        "fanout_join_echoes_breach",
        "retry_loop_recovered",
        "retry_loop_unrecovered",
        "retry_runaway",
    ]


def test_the_injected_culprit_table_is_checked_against_the_traces():
    """The ground-truth table must be a FACT about the payloads, not a constant.

    Pins the review finding that ``assert INJECTED_CULPRIT[cell] == culprit``
    compared two constants from this file and therefore verified nothing. Here
    the trace files are re-read: a cell the table calls clean must contain no
    contract rewrite at all, and a cell with a named culprit must show that
    agent's own span rewriting the key.
    """
    for cell, culprit in INJECTED_CULPRIT.items():
        diffs = agents_with_a_contract_diff(cell)
        if culprit is None:
            assert diffs == {}, f"{cell}: table says clean, payload disagrees"
        else:
            assert culprit in diffs, f"{cell}: no rewrite at the named culprit"
            assert diffs[culprit] == ("markdown", "html")


def test_the_echo_cell_really_contains_two_nodes_with_a_rewrite():
    """The premise of the fan-IN question, stated as payload fact.

    ``fanout_join_echoes_breach`` exists because the original fan-out cell could
    not ask it: there, the joiner drops the contract key, so "the lattice does
    not blame the joiner" was a property of the authored payload rather than of
    the lattice. Here BOTH the branch and the joiner show `format: markdown ->
    html` in their own spans — the discrimination has to come from the engine.
    """
    assert set(agents_with_a_contract_diff("fanout_join_echoes_breach")) == {
        "write_specs",
        "merge",
    }
    # ... and in the original cell it does not: only one node is even eligible.
    assert set(agents_with_a_contract_diff("fanout_branch_fault")) == {"write_specs"}


@pytest.mark.parametrize(
    "cell,primary,edge_types",
    [
        # Fan-out + fan-in is a DAG, not a star: the orchestrator spawns the
        # joiner as well as the branches, so the joiner has an extra in-edge.
        # The fan-IN edges are TOOL_DELEGATION — span parenting cannot express
        # them (one parent per span), which is why a join needs a tool span.
        ("fanout_branch_fault", "dag", {"SPAWN", "TOOL_DELEGATION"}),
        ("fanout_join_echoes_breach", "dag", {"SPAWN", "TOOL_DELEGATION"}),
        ("retry_loop_unrecovered", "pipeline_with_feedback", {"SPAWN", "TOOL_DELEGATION"}),
        ("retry_loop_recovered", "pipeline_with_feedback", {"SPAWN", "TOOL_DELEGATION"}),
        ("retry_runaway", "pipeline_with_feedback", {"SPAWN", "TOOL_DELEGATION"}),
        # Four peers, no span parentage at all: every edge is an A2A message.
        ("a2a_diamond_fault", "star", {"A2A_MESSAGE"}),
    ],
)
def test_each_cell_classifies_as_its_archetype(cell, primary, edge_types):
    meta = CORPUS[cell]
    edges = [(u, v) for u, v, _t in meta["edges"]]
    assert classify_topology(meta["nodes"], edges)["primary"] == primary
    assert {t for _u, _v, t in meta["edges"]} == edge_types


@pytest.mark.parametrize(
    "cell,handoff,target",
    [
        ("retry_loop_unrecovered", "handoff_revise", "revise"),
        ("retry_loop_recovered", "handoff_revise", "revise"),
        ("retry_runaway", "handoff_revise_11", "revise_11"),
    ],
)
def test_the_retry_back_edge_is_a_name_reference_not_a_causal_order(
    cell, handoff, target
):
    """DISCLOSURE, asserted rather than footnoted: the loop cells' back-edge
    contradicts its own timestamps.

    The TOOL span that closes each cycle ENDS BEFORE the agent span it names
    STARTS — the delegation "returns" up to 1.5s (retry cells) or 9.7s (runaway)
    before its target begins. The mapper builds the edge anyway, because rule 2
    resolves TOOL_DELEGATION by agent NAME with no temporal constraint. So what
    the cycle tests below pin is "the mapper closes a cycle from a name
    reference", NOT "a causally ordered retry trace forms an SCC". Anyone
    reading them as evidence that real retry loops reconstruct as SCCs is
    reading more than the corpus contains — the caveat about per-attempt agent
    names understated this, and prose can be skipped, so it lives here as a
    failing-if-false assertion.
    """
    payload = json.loads(
        (_TRACES / CORPUS[cell]["file"]).read_text(encoding="utf-8")
    )
    spans = {}
    for resource in payload["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                spans[span["name"]] = (
                    int(span["startTimeUnixNano"]),
                    int(span["endTimeUnixNano"]),
                )
    _handoff_start, handoff_end = spans[handoff]
    target_start, _target_end = spans[target]
    assert handoff_end < target_start, (
        f"{cell}: the back-edge is now causally ordered — re-read the cycle "
        "tests, they may finally mean what they appear to mean"
    )


def test_the_retry_cells_really_contain_a_cycle():
    """A retry loop that is not an SCC is just a longer pipeline. Both retry
    cells must condense to one non-trivial component, or the loop machinery
    (exit-node scoring, intra-SCC drill, iteration limits) is never exercised.

    Read with ``test_the_retry_back_edge_is_a_name_reference_not_a_causal_order``:
    the cycle these cells contain is one the MAPPER builds from a name
    reference."""
    for cell, size in (
        ("retry_loop_unrecovered", 3),
        ("retry_loop_recovered", 3),
        ("retry_runaway", 11),
    ):
        meta = CORPUS[cell]
        topo = classify_topology(meta["nodes"], [(u, v) for u, v, _t in meta["edges"]])
        assert topo["scc_count"] == 1, cell
        inp = blame_input(cell, {})
        members = max(
            (sn.members for sn in condense(inp).super_nodes.values()), key=len
        )
        assert len(members) == size, (cell, members)


# ---------------------------------------------------------------------------
# Layer B — localization over the reconstructed graphs
# ---------------------------------------------------------------------------

# (a) FAN-OUT ----------------------------------------------------------------


def test_fanout_blames_the_poisoned_branch_not_the_join():
    """One branch degrades; the join drops because it merged that branch. The
    join is a DESCENDANT of the origin, so it is inherited, never a second
    cause, and the healthy siblings are not on the propagation path."""
    report = find_blame(
        blame_input(
            "fanout_branch_fault",
            {
                "orchestrator": None,
                "write_intro": 0.9,
                "write_specs": 0.2,
                "write_pricing": 0.9,
                "merge": 0.3,
            },
            _BAD_TERMINAL,
        )
    )
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["write_specs"]
    assert report.propagation_path == ["write_specs", "merge"]
    assert verdict_for(report, "merge") == "inherited"
    assert verdict_for(report, "write_intro") == "healthy"
    assert verdict_for(report, "write_pricing") == "healthy"
    assert report.evidence.topology["primary"] == "dag"
    assert report.evidence.topology["max_fan_out"] == 4
    # In THIS trace the branch's only predecessor is an orchestrator wrapper the
    # generator gave no output.value, so it is unscored: the "quality was fine
    # going in" baseline is ASSUMED, not observed, and the verdict is
    # `origin_boundary` at the capped 0.60 — not the `origin_drop` / 0.84 that
    # test_fanout_golden gets by handing its orchestrator a score of 0.9.
    # SCOPE: that is a consequence of the payload-less root, which is a modelling
    # choice here (a realistic one — a wrapper that only dispatches has no
    # content to score), NOT something a fan-out shape forces. A trace whose root
    # emits a scored output would measure a real drop and land higher. What
    # generalises is the rule, not the number: an assumed baseline is capped.
    assert verdict_for(report, "write_specs") == "origin_boundary"
    assert report.confidence == pytest.approx(0.6)


def test_fanout_with_healthy_branches_blames_the_merge():
    """Every branch is fine and the merged document is not: the MERGE broke it.
    No branch may be blamed for a fault that appeared only at the join."""
    report = find_blame(
        blame_input(
            "fanout_branch_fault",
            {
                "orchestrator": None,
                "write_intro": 0.9,
                "write_specs": 0.9,
                "write_pricing": 0.9,
                "merge": 0.2,
            },
            _BAD_TERMINAL,
        )
    )
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["merge"]
    for healthy in ("write_intro", "write_specs", "write_pricing"):
        assert verdict_for(report, healthy) == "healthy"


def test_fanout_two_independent_branches_is_multi_culprit():
    """Two branches fail with no shared non-root ancestry: neither shadows the
    other. Reporting one of them would silently drop a real, independent fault."""
    report = find_blame(
        blame_input(
            "fanout_branch_fault",
            {
                "orchestrator": None,
                "write_intro": 0.2,
                "write_specs": 0.9,
                "write_pricing": 0.2,
                "merge": 0.3,
            },
            _BAD_TERMINAL,
        )
    )
    assert report.report_type == "multi_culprit"
    assert sorted(report.culprit_run_ids) == ["write_intro", "write_pricing"]
    assert verdict_for(report, "write_specs") == "healthy"
    assert verdict_for(report, "merge") == "inherited"


# (b) RETRY LOOP -------------------------------------------------------------


def test_retry_loop_that_never_recovers_blames_the_loop_exit():
    """draft -> qa -> revise -> draft, and the revision that finally left the
    cycle is the bad one. The SCC's score is its EXIT member's (that is what
    flows downstream), so the origin must be `revise` — not the whole cycle and
    not `publish`, which merely shipped what it was handed."""
    report = find_blame(
        blame_input(
            "retry_loop_unrecovered",
            {
                "orchestrator": None,
                "draft": 0.8,
                "qa": 0.8,
                "revise": 0.2,
                "publish": 0.3,
            },
            _BAD_TERMINAL,
        )
    )
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["revise"]
    assert verdict_for(report, "publish") == "inherited"
    assert report.evidence.topology["primary"] == "pipeline_with_feedback"


def test_retry_loop_that_recovers_is_not_a_live_break():
    """The first draft was weak, the loop fixed it, the deliverable is fine.
    That is what a retry loop is FOR: it must report `degraded_recovered` (a
    fragile node, a near-miss) and never a cut_point — otherwise every
    successful retry in production reads as a failure."""
    report = find_blame(
        blame_input(
            "retry_loop_recovered",
            {
                "orchestrator": None,
                "draft": 0.3,
                "qa": 0.8,
                "revise": 0.9,
                "publish": 0.9,
            },
            _OK_TERMINAL,
        )
    )
    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["draft"]
    assert verdict_for(report, "draft") == "degraded_recovered"
    assert verdict_for(report, "publish") == "healthy"
    # The typed defect keeps its origin but is marked recovered — that flag is
    # what stops derive_report_type from projecting a cut_point.
    content = [d for d in report.evidence.defects if d["kind"] == "content"]
    assert len(content) == 1 and content[0]["recovered"] is True


def test_a_recovered_retry_still_pages_under_the_same_key_as_a_live_break():
    """RECORDED NUANCE, not an assertion that this is right.

    ``degraded_recovered`` is rendered "PASSED — with warnings", but
    ``derive_incident`` maps it to the SAME incident key a cut_point gets. So a
    retry loop that worked is indistinguishable from a live quality break to
    anything reading incident_key (alert routing, the CLI exit code). The
    report_type carries the distinction; the incident does not."""
    assert derive_incident("degraded_recovered", [], False) == derive_incident(
        "cut_point", [], False
    ) == ("degraded_quality", "degraded_quality")


def test_runaway_retry_is_a_loop_defect_over_every_attempt():
    """Eleven revision attempts in one SCC, every one of them scoring fine. No
    single node "broke quality" — the DEFECT is that the loop ran past the
    limit, so the culprits are its members and the confidence is that of a
    deterministic limit breach, not of a judged score."""
    nodes = CORPUS["retry_runaway"]["nodes"]
    report = find_blame(
        blame_input(
            "retry_runaway",
            {n: (None if n == "orchestrator" else 0.9) for n in nodes},
        )
    )
    assert report.report_type == "loop_detected"
    assert report.culprit_run_ids == [f"revise_{i}" for i in range(1, 12)]
    assert report.confidence == 1.0
    loop = [d for d in report.evidence.defects if d["kind"] == "loop"]
    assert len(loop) == 1
    assert loop[0]["channel"] == "deterministic"
    assert report.evidence.loop_anomalies[0].iterations == 11
    assert report.evidence.loop_anomalies[0].limit_kind == "max_iterations"


# (c) A2A --------------------------------------------------------------------


def test_a2a_diamond_blames_the_bad_peer_branch():
    """Four peers, edges built purely from A2A messages: retriever feeds both
    analyst and translator, both feed editor. The translator branch is bad. The
    verdict must name the translator, leave the analyst healthy and mark the
    editor inherited — the same discrimination as the fan-out, over a completely
    different edge type."""
    report = find_blame(
        blame_input(
            "a2a_diamond_fault",
            {"retriever": 0.9, "analyst": 0.9, "translator": 0.2, "editor": 0.3},
            _BAD_TERMINAL,
        )
    )
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["translator"]
    assert report.propagation_path == ["translator", "editor"]
    assert verdict_for(report, "analyst") == "healthy"
    assert verdict_for(report, "retriever") == "healthy"
    assert verdict_for(report, "editor") == "inherited"
    assert report.evidence.topology["primary"] == "star"


# --- the thesis itself ------------------------------------------------------


@pytest.mark.parametrize(
    "cell,scores,expected_culprits,inherited",
    [
        (
            "fanout_branch_fault",
            {
                "orchestrator": None,
                "write_intro": 0.9,
                "write_specs": 0.2,
                "write_pricing": 0.9,
                "merge": 0.3,
            },
            ["write_specs"],
            ["merge"],
        ),
        (
            "retry_loop_unrecovered",
            {
                "orchestrator": None,
                "draft": 0.8,
                "qa": 0.8,
                "revise": 0.2,
                "publish": 0.3,
            },
            ["revise"],
            ["publish"],
        ),
        (
            "a2a_diamond_fault",
            {"retriever": 0.9, "analyst": 0.9, "translator": 0.2, "editor": 0.3},
            ["translator"],
            ["editor"],
        ),
    ],
)
def test_graph_first_beats_node_isolation(cell, scores, expected_culprits, inherited):
    """The graph-vs-isolation claim, stated as a measurable difference.

    A node-in-isolation reading of the very same score vector flags every node
    under the threshold — on all three topologies that is the origin PLUS the
    node(s) it poisoned downstream. Reading the scores as a GRAPH keeps the
    origin and demotes the rest to `inherited`. If this ever collapses to
    equality, the product's central claim has no measurable content on that
    shape.

    SCOPE (do not read this as fault→blame): the scores are authored here, the
    injected `format` rewrite contributes nothing to them, and no judge ran. What
    is under test is the lattice's reading of a score vector over an edge list a
    real OTLP payload produced — the edges are the new thing, not the scoring.
    The fault→verdict claim is exercised in the deterministic end-to-end section.
    """
    threshold = BlameConfig().threshold
    below = {n for n, s in scores.items() if s is not None and s < threshold}
    report = find_blame(blame_input(cell, scores, _BAD_TERMINAL))

    assert report.culprit_run_ids == expected_culprits
    assert set(expected_culprits) < below, "no discrimination to make on this cell"
    assert below - set(report.culprit_run_ids) == set(inherited)
    for run_id in inherited:
        assert verdict_for(report, run_id) == "inherited"


# ---------------------------------------------------------------------------
# End-to-end: the DETERMINISTIC channel, no judge anywhere
# ---------------------------------------------------------------------------


def _e2e_input(cell: str) -> tuple[BlameInput, dict[str, str]]:
    """The trace file, through the mapper and the real deterministic checks.

    Not a re-implementation: ``worker.scoring.contract_violations`` is the exact
    function tier2 calls per node. What is skipped is only the judged component
    (there is no LLM in this suite), which is why every score comes out None —
    the state a real ``detective analyze --no-judge`` run produces.
    """
    bundle_mod = pytest.importorskip("detective_cli.bundle")
    scoring = pytest.importorskip("worker.scoring")

    meta = CORPUS[cell]
    exports = bundle_mod.load_trace(_TRACES / meta["file"])
    bundle = bundle_mod.bundles_from_exports(exports, a2a_detection=meta["a2a"])[0]
    names = {str(r.run_id): (r.agent_name or str(r.run_id)) for r in bundle.runs}

    scores = {}
    for run in bundle.runs:
        run_id = str(run.run_id)
        violations = tuple(
            scoring.contract_violations(run.input_inline, run.output_inline)
        )
        scores[run_id] = NodeScore(
            run_id=run_id,
            score=None,
            components={"schema": None, "judge": None, "heuristics": None},
            input_flawed=None,
            unscored_reason=(
                "payload_missing"
                if not (run.output_inline or "").strip()
                else "insufficient_components"
            ),
            judge_note=None,
            contract_violations=violations,
        )

    graph_ops = pytest.importorskip("worker.graph_ops")
    inp = graph_ops.build_blame_input(bundle, scores, None, {}, BlameConfig())
    return inp, names


@pytest.mark.parametrize(
    "cell,culprit",
    [
        ("fanout_branch_fault", "write_specs"),
        ("fanout_join_echoes_breach", "write_specs"),
        ("retry_loop_unrecovered", "revise"),
        ("a2a_diamond_fault", "translator"),
    ],
)
def test_deterministic_channel_localizes_the_injection_without_a_judge(cell, culprit):
    """No LLM in the path at all, and the injected fault is still pinned.

    The injection is a silently rewritten carried parameter (`format`:
    markdown -> html), which is point-attributable: the node's own input/output
    diff observed it arrive intact and leave rewritten. The cut-point lattice
    must return exactly that node — on a fan-out branch, on a loop exit and
    across an A2A hop alike.

    NOT claimed here (it was, and it was wrong): that the lattice "refuses to
    blame the joiner". In ``fanout_branch_fault`` the joiner drops the contract
    key, so it has no diff and could never be a candidate — that discrimination
    is supplied by the payload. ``fanout_join_echoes_breach`` is the cell where
    the joiner DOES carry a diff and the engine has to decide; see
    ``test_a_joiner_that_echoes_the_rewritten_value_is_not_a_second_origin``.

    The culprit is ground truth read from the payload, not from a constant —
    ``test_the_injected_culprit_table_is_checked_against_the_traces`` is what
    ties the parameter below to the trace file.
    """
    inp, names = _e2e_input(cell)
    candidates = select_candidates(inp)
    assert [(names[c.run_id], c.via) for c in candidates] == [
        (culprit, "deterministic")
    ]


def test_a_joiner_that_echoes_the_rewritten_value_is_not_a_second_origin():
    """THE fan-IN question, on the cell built to ask it (review finding D1).

    Both ``write_specs`` and ``merge`` show `format: markdown -> html` in their
    own spans (pinned by test_the_echo_cell_really_contains_two_nodes_with_a
    _rewrite), and the joiner's input is UNAMBIGUOUS — it was handed the task's
    markdown contract, so nothing about its own diff says "inherited". Only the
    graph does: html was already in circulation from an ancestor, so the joiner
    echoed a value it did not invent.

    Before the R2 basis rule learned the output side, ``_fresh_contract_origin``
    compared the joiner's INPUT value against ancestor rewrites, found no
    ancestor rewriting TO markdown, and declared it a fresh origin — a second
    deterministic culprit for a fault it merely carried, and a multi_culprit
    verdict on a single-fault run. That is what this pins.
    """
    inp, names = _e2e_input("fanout_join_echoes_breach")
    assert [
        (names[c.run_id], c.via) for c in select_candidates(inp)
    ] == [("write_specs", "deterministic")]

    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert [names[c] for c in report.culprit_run_ids] == ["write_specs"]
    # The joiner's breach is still REPORTED as evidence — suppressing it as an
    # origin must not delete the measurement.
    assert sorted(
        names[v["run_id"]] for v in report.evidence.contract_violations
    ) == ["merge", "write_specs"]


@pytest.mark.parametrize("cell", ["retry_loop_recovered", "retry_runaway"])
def test_clean_cells_raise_no_deterministic_origin(cell):
    """The negative control. A retry loop that recovered, and a loop that only
    ran long, carry no content or contract fault — the deterministic channel
    must stay silent on both, or every non-linear run would come out guilty."""
    inp, _names = _e2e_input(cell)
    assert select_candidates(inp) == []
    assert agents_with_a_contract_diff(cell) == {}


@pytest.mark.parametrize(
    "cell,culprit",
    [
        ("fanout_branch_fault", "write_specs"),
        ("fanout_join_echoes_breach", "write_specs"),
        ("retry_loop_unrecovered", "revise"),
        ("a2a_diamond_fault", "translator"),
    ],
)
def test_a_deterministic_origin_reaches_the_report_with_no_judged_score(cell, culprit):
    """WAS A STRICT XFAIL — the recorded gap this corpus was built to expose.

    ``find_blame``'s first cascade row was ``all(s is None for s in
    score_map.values()) -> note('no_scores')``, and it ran BEFORE the candidate
    list was consulted. So a trace carrying a real, DETECTED contract breach —
    select_candidates returns the node, the breach sits in
    evidence.contract_violations — came out `unclassified`, culprits [],
    confidence 0.0, and the CLI printed "NOT VERIFIED · nothing could be
    measured" and exited 0. The row assumed "no quality score" means "no
    evidence", which is exactly what the deterministic channel exists to
    disprove.

    The verdict now has to be honest in both directions at once: the defect IS
    localized (observation and attribution near-certain, because the input/output
    diff observed origination), and the content channel measured NOTHING — no
    score, no terminal, so no claim about quality may be made.
    """
    inp, names = _e2e_input(cell)
    report = find_blame(inp)

    assert [names[c] for c in report.culprit_run_ids] == [culprit]
    assert report.report_type == "cut_point"
    assert report.confidence == pytest.approx(0.95)
    assert report.evidence.observation_confidence == pytest.approx(0.95)
    assert report.evidence.attribution_confidence == pytest.approx(0.95)

    defects = report.evidence.defects
    assert [d["kind"] for d in defects] == ["contract"]
    contract = defects[0]
    assert contract["channel"] == "deterministic"
    assert contract["origin"] == {
        "kind": "Localized",
        "run_id": run_id_of(names, culprit),
    }
    # The caveats carry the other half of the truth: nothing in the content
    # channel was measured, so nothing about content quality is being claimed.
    assert contract["unverified_in_channel"] == "content"
    assert contract["quality_unmeasured"] is True
    assert contract["recovered"] is False


def test_a_deterministic_only_verdict_is_never_sold_as_a_recovered_near_miss():
    """The regression that hid inside the fix: `degraded_recovered`.

    With the cascade row fixed, the contract defect stood alone and
    ``derive_report_type`` projected it as `degraded_recovered` — a verdict that
    the CLI renders "PASSED — with warnings" and whose note asserts "every
    successor scored healthy and the terminal deliverable is ok". On a run with
    no scores and no terminal verdict, that is the absence of a measurement
    reported as a passing one. The projection now requires the CONTENT channel to
    have actually measured something before it may call a breach recovered.
    """
    inp, names = _e2e_input("fanout_branch_fault")
    report = find_blame(inp)
    origin = run_id_of(names, "write_specs")
    assert report.report_type != "degraded_recovered"
    # The origin's candidacy states the deterministic basis instead of the
    # "unscored — never a candidate" line that used to contradict the headline...
    assert verdict_for(report, origin) == "origin_deterministic"
    # ... and it does not invent a judged score for a node no judge ever saw.
    assert report.evidence.candidacy_records[origin]["data"]["score"] is None


def test_the_measured_breach_is_still_in_the_evidence_stream():
    """The evidence stream was never the problem — pinned as fact rather than as
    prose: the contract breach IS recorded against the right node with the right
    values, which is what made the old `unclassified` verdict a discarded
    measurement rather than a failure to detect."""
    inp, names = _e2e_input("fanout_branch_fault")
    report = find_blame(inp)
    breaches = [
        (names[v["run_id"]], v["key"], v["from"], v["to"])
        for v in report.evidence.contract_violations
    ]
    assert breaches == [("write_specs", "format", "markdown", "html")]
    assert report.evidence.topology["primary"] == "dag"


# ---------------------------------------------------------------------------
# What the SHIPPED SDK can express — the corpus had to work around both of these
# ---------------------------------------------------------------------------


def _sdk_graph(build) -> tuple[list[str], list[tuple[str, str]], dict]:
    """Run ``build(r)`` under the shipped ``detective_sdk.tracing`` API and map
    the exported trace. Returns (agent names, edges as names, topology)."""
    import json
    import tempfile

    tracing = pytest.importorskip("detective_sdk.tracing")
    mapper = pytest.importorskip("otel_mapper")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trace.json"
        with tracing.run("orchestrator", task={"goal": "x"}, trace_file=str(path)) as r:
            build(r)
        payload = json.loads(path.read_text(encoding="utf-8"))

    result = mapper.map_spans(mapper.flatten_export_request(payload))
    names = {c.run_key: (c.agent_name or c.run_key) for c in result.runs}
    edges = [(names[e.from_run_key], names[e.to_run_key]) for e in result.edges]
    topology = classify_topology(
        list(names), [(e.from_run_key, e.to_run_key) for e in result.edges]
    )
    return [names[c.run_key] for c in result.runs], sorted(edges), topology


def test_sdk_tracing_api_cannot_express_a_fan_in():
    """FINDING: ``run``/``step``/``span`` produces fan-OUT but never fan-IN.

    A span carries one ``parentSpanId``, and that is the only structure the
    ergonomic API emits — so three workers and a joiner come out as four
    siblings of the orchestrator with NO edge from any worker into the join.
    Graph-first blame then has nothing to follow: the join cannot be `inherited`
    from a branch it is not connected to. The corpus builds its joins from TOOL
    delegation spans instead, which the API has no method for (``Span`` hard-
    codes ``openinference.span.kind=AGENT``); only the generic ``attr()`` escape
    hatch reaches it, and only if the integrator knows the mapper's attribute
    contract by heart.
    """

    def build(r):
        for name in ("w1", "w2", "join"):
            with r.span(name) as s:
                s.output = {"node": name}

    nodes, edges, _topology = _sdk_graph(build)
    assert sorted(nodes) == ["join", "orchestrator", "w1", "w2"]
    assert edges == [
        ("orchestrator", "join"),
        ("orchestrator", "w1"),
        ("orchestrator", "w2"),
    ]
    assert ("w1", "join") not in edges and ("w2", "join") not in edges

    # The escape hatch does reach it — recorded so the finding stays precise:
    # this is a missing API, not a missing capability.
    def build_with_delegation(r):
        with r.span("w1") as s:
            s.output = {"node": "w1"}
        with r.span("join") as s:
            s.output = {"node": "join"}
            with r.span("collect") as t:
                t.attr("openinference.span.kind", "TOOL")
                t.attr("gen_ai.tool.target_agent", "w1")

    _nodes, edges, _topology = _sdk_graph(build_with_delegation)
    assert ("w1", "join") in edges


def test_sdk_tracing_api_flattens_a_retry_loop_into_a_pipeline():
    """FINDING: a retry loop written with ``r.step()`` is not a loop.

    Three write/qa attempts reconstruct as a SEVEN-node chain with
    ``scc_count == 0`` — classified `pipeline`. Nothing in the engine's loop
    machinery (iteration limits, exit-node scoring, the intra-SCC drill) can
    ever fire on it: what is missing is the BACK-EDGE, and only ``r.retry()``
    emits one.

    Updated when the SDK started disambiguating colliding agent names: the
    attempts now ship as write#1/qa#1/write#2/… and therefore chain. Before that
    they collided on one ``gen_ai.agent.name``, the mapper dropped SPAWN between
    them, and the sequence vanished from the graph entirely. Half of this finding
    is therefore fixed at the source; the half that remains — a chain is not a
    cycle — is what this test still pins.
    """

    def build(r):
        for i in range(3):
            with r.step("write") as s:
                s.output = {"draft": i}
            with r.step("qa") as s:
                s.output = {"verdict": "reject" if i < 2 else "accept"}

    nodes, _edges, topology = _sdk_graph(build)
    assert [n for n in nodes if n.startswith("write")] == ["write#1", "write#2", "write#3"]
    assert [n for n in nodes if n.startswith("qa")] == ["qa#1", "qa#2", "qa#3"]
    # The half that remains broken without `retry`: no back-edge, so no cycle.
    assert topology["scc_count"] == 0
    assert topology["primary"] == "pipeline"
    assert topology["node_count"] == 7


def test_full_pipeline_pages_every_cell_that_carries_a_real_fault():
    """The whole local pipeline (tier1 -> tier2, in-process, no judge) over every
    cell, so the corpus records what a user actually sees.

    This is the end-to-end shape of the cascade-row fix. Before it, the four
    cells with a detected contract breach paged for NOTHING — their reports were
    `unclassified` and the CLI exited 0 with "NOT VERIFIED" — and only
    ``retry_runaway`` raised an incident, through tier1's deterministic
    ``loop_anomaly`` flag rather than through blame. Now each breach cell raises
    a `degraded_quality` incident off its own deterministic evidence, and the
    genuinely clean retry stays silent (no judge ran, so there is nothing to say
    about it — absence of evidence must not become an incident either).
    """
    analyze_mod = pytest.importorskip("detective_cli.analyze")
    bundle_mod = pytest.importorskip("detective_cli.bundle")

    paged: dict[str, str | None] = {}
    types: dict[str, str | None] = {}
    for cell, meta in sorted(CORPUS.items()):
        exports = bundle_mod.load_trace(_TRACES / meta["file"])
        bundles = bundle_mod.bundles_from_exports(exports, a2a_detection=meta["a2a"])
        run = analyze_mod.analyze(
            bundles, settings=analyze_mod.local_settings(), no_judge=True
        )
        assert run.judge_enabled is False
        graph = run.graphs[0]
        # Every node unscored: with no judge the composite never clears
        # SCORE_MIN_WEIGHT. That is the state the fixed cascade row has to
        # localize through.
        assert all(row.quality_score is None for row in graph.node_scores.values())
        paged[cell] = (graph.incident or {}).get("incident_key")
        types[cell] = (graph.blame_report or {}).get("report_type")

    assert paged == {
        "a2a_diamond_fault": "degraded_quality",
        "fanout_branch_fault": "degraded_quality",
        "fanout_join_echoes_breach": "degraded_quality",
        "retry_loop_recovered": None,
        "retry_loop_unrecovered": "degraded_quality",
        "retry_runaway": "loop_detected",
    }
    assert types == {
        "a2a_diamond_fault": "cut_point",
        "fanout_branch_fault": "cut_point",
        "fanout_join_echoes_breach": "cut_point",
        # Nothing was measured on this run at all: no judge, no breach, no loop.
        # The engine answers `unclassified` and the CLI attaches no blame report
        # at all, rendering NOT VERIFIED — an absence, never a pass.
        "retry_loop_recovered": None,
        "retry_loop_unrecovered": "cut_point",
        # The limit breach is deterministic evidence too — it now reaches the
        # report instead of being discarded with the scores.
        "retry_runaway": "loop_detected",
    }
