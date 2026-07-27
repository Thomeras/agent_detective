"""Topological fuzzer: invariants that must hold for ANY graph the engine sees.

This file used to hold ONE property: a random DAG (edges only i<j, so never a
cycle) with exactly one injected fault, every descendant decayed and never
recovering, everything else healthy — a shape in which, as its own docstring
admitted, "detection is therefore guaranteed". It could catch a gross
regression; it could not catch a localization bug, because it never generated
the space where localization is hard: cycles, joins with mixed-health inputs,
unscored nodes, multiple sinks, verifiers.

The replacement generates that space and asserts INVARIANTS rather than an
oracle. For an arbitrary graph you cannot demand "the culprit is node X" — the
honest answer is sometimes "not localizable". But these must hold always:

1. ``test_verdict_never_contradicts_its_own_evidence`` — a report may not state
   something its own score map / graph refutes.
2. ``test_a_degraded_node_is_never_reported_as_healthy`` — every sub-threshold
   node is accounted for.
3. ``test_no_confident_wrong_answer`` — with one clean injection the engine
   names that node or admits it cannot localize; never a DIFFERENT node with
   high confidence.
4. ``test_report_is_invariant_under_input_permutation`` — shuffling the input
   lists cannot change the verdict.
5. ``test_lowering_an_unrelated_node_does_not_move_blame`` — degrading a node
   causally unrelated to a measured origin may add origins, never move one.

What they caught on first contact, none of it hypothetical:

- ``find_blame`` raising ``ValueError`` (defect with no supporting finding) on a
  cycle member sitting exactly AT the threshold — an analysis that dies rather
  than reports;
- two sources of NON-DETERMINISM, both keyed on ``nx.condensation``'s
  component ids, which are assigned in edge-insertion order: the propagation
  path on a diamond, and the degradation chain when two successors tie on score.
  The same run gave different answers depending on the order the exporter
  happened to emit its spans — under a README that promises determinism.

Invariants 1 and 2 also fail on the pre-fix engine (the cycle blind spot and the
composition_failure guard reading super-nodes) within a handful of examples.
"""

import networkx as nx
import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from blame_engine import (
    BlameConfig,
    BlameInput,
    NodeScore,
    TerminalVerdict,
    find_blame,
    select_candidates,
)

THRESHOLD = BlameConfig().threshold

# 200 keeps the file near 2s (~18s under coverage instrumentation) — a unit-suite
# budget. The bug-hunting pass is this same file at max_examples=4000 (~45s):
# every defect these properties have found so far surfaced within that budget, so
# raise the number here temporarily rather than maintaining a second harness.
SETTINGS = settings(
    max_examples=200,
    derandomize=True,          # seeded: a failure is reproducible from the file alone
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# --- generators -----------------------------------------------------------


@st.composite
def topology(draw, min_nodes: int = 3, max_nodes: int = 9):
    """An arbitrary execution-graph shape.

    Each node picks 0–2 parents from the nodes before it, which yields sources,
    fan-out, joins and multiple sinks for free; then 0–2 BACK edges turn parts of
    it into cycles (retry loops, orchestrator↔sub-agent delegation, peer meshes).
    Nodes with no parents and no children are isolated components — the
    header-correlated forest a graph gets when edges were never instrumented.
    """
    n = draw(st.integers(min_nodes, max_nodes))
    nodes = [f"n{i}" for i in range(n)]
    edges: set[tuple[str, str]] = set()
    for j in range(1, n):
        parents = draw(
            st.lists(st.integers(0, j - 1), min_size=0, max_size=2, unique=True)
        )
        edges.update((f"n{p}", f"n{j}") for p in parents)
    for _ in range(draw(st.integers(0, 2))):
        a = draw(st.integers(1, n - 1))
        b = draw(st.integers(0, a - 1))
        edges.add((f"n{a}", f"n{b}"))
    return nodes, sorted(edges)


_SCORE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def blame_input(draw):
    """A fully arbitrary analysis input: any shape, any scores (including
    unknown), any terminal verdict, with or without a verifier node."""
    nodes, edges = draw(topology())
    scores: dict[str, NodeScore] = {}
    for nd in nodes:
        # ~1 in 8 nodes is UNSCORED (judge error / missing payload) — the case
        # that decides whether a fallback verdict is allowed to fire at all.
        if draw(st.integers(0, 7)) == 0:
            scores[nd] = NodeScore(
                run_id=nd, score=None, components={}, input_flawed=None,
                unscored_reason=draw(st.sampled_from(["judge_error", "payload_missing"])),
                judge_note=None,
            )
        else:
            scores[nd] = NodeScore(
                run_id=nd, score=round(draw(_SCORE), 2), components={},
                input_flawed=draw(st.sampled_from([None, False, True])),
                unscored_reason=None, judge_note=None,
            )
    # Agent names: producers, plus sometimes one verifier — the role split
    # changes which lane a node is judged in and who can open a gap.
    agent_names = {nd: nd for nd in nodes}
    if draw(st.booleans()) and nodes:
        agent_names[draw(st.sampled_from(nodes))] = "qa"
    tv = draw(
        st.sampled_from(
            [
                None,
                TerminalVerdict(bad=False, score=0.95, reasoning="ok"),
                TerminalVerdict(bad=True, score=0.2, reasoning="bad"),
                TerminalVerdict(bad=True, score=0.2, reasoning="bad", checkable=False),
            ]
        )
    )
    return _build(nodes, edges, scores, agent_names, tv)


def _build(nodes, edges, scores, agent_names, tv) -> BlameInput:
    return BlameInput(
        nodes=list(nodes),
        edges=list(edges),
        scores=dict(scores),
        node_costs={nd: 1.0 for nd in nodes},
        # Keyed off the node's NAME, never its position in the list: end times are
        # per-run facts, so deriving them from list order would make the
        # permutation test change the input's meaning instead of its
        # representation (and it would silently pass by measuring nothing).
        node_end_times={nd: float(nd[1:]) for nd in nodes},
        agent_names=dict(agent_names),
        error_span_ids={},
        terminal_verdict=tv,
        loop_baselines={},
        config=BlameConfig(),
    )


def _graph(inp: BlameInput) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_nodes_from(inp.nodes)
    known = set(inp.nodes)
    g.add_edges_from((u, v) for u, v in inp.edges if u in known and v in known)
    return g


def _scored(report) -> dict[str, float]:
    return {k: v for k, v in report.evidence.score_map.items() if v is not None}


# --- 1. the verdict may not contradict its own evidence -------------------


@SETTINGS
@given(blame_input())
def test_verdict_never_contradicts_its_own_evidence(inp: BlameInput) -> None:
    report = find_blame(inp)
    scored = _scored(report)
    graph = _graph(inp)
    culprits = set(report.culprit_run_ids)

    for rec in report.evidence.note_records:
        # composition_failure's headline claim ("all scores above threshold"),
        # checked against the map the same report renders. It used to read
        # super-node scores (a cycle is scored by its EXIT), so a member at 0.10
        # sat in the score map under a sentence saying every node was healthy.
        if rec["slug"] == "composition_failure":
            low = {k: v for k, v in scored.items() if v < THRESHOLD}
            assert not low, f"claimed all-healthy, score map has {low}"

    for node, rec in report.evidence.candidacy_records.items():
        # "Inherited, shadowed by the origin upstream" is a claim about the
        # GRAPH: it points the reader at an origin above this node. There must
        # be one. The claim is now the verdict CODE, so no wording can dodge it.
        if rec["verdict"] == "inherited":
            ancestors = nx.ancestors(graph, node)
            assert culprits & ancestors, (
                f"{node} called shadowed, but no culprit is an ancestor "
                f"(culprits={sorted(culprits)})"
            )

    # A culprit the report names must exist in the graph it analysed.
    assert culprits <= set(inp.nodes)
    # Confidence is only meaningful with someone to be confident about.
    if not report.culprit_run_ids:
        assert report.confidence == 0.0


# --- 2. every degraded node is accounted for ------------------------------


@SETTINGS
@given(blame_input())
def test_a_degraded_node_is_never_reported_as_healthy(inp: BlameInput) -> None:
    """The candidacy trace is the report's audit surface: a node below the
    quality threshold may be excluded for many honest reasons, but 'healthy' is
    not one of them. This is the safety net under the cycle blind spot — a
    sub-threshold member the verdict never mentions has to show up somewhere."""
    report = find_blame(inp)
    for node, score in _scored(report).items():
        if score < THRESHOLD:
            rec = report.evidence.candidacy_records[node]
            assert rec["verdict"] != "healthy", f"{node} at {score}: {rec!r}"


# --- 3. no confident wrong answer ----------------------------------------


@st.composite
def single_injection(draw):
    """A healthy graph with ONE node degraded, its descendants decayed so the
    damage visibly propagates and nothing recovers. Unlike the old version this
    runs over cycles and joins too, so the fault can sit inside an SCC or feed a
    merge — exactly where localization used to go wrong."""
    nodes, edges = draw(topology())
    graph = nx.DiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from(edges)

    fault = draw(st.sampled_from(nodes))
    fault_score = round(draw(st.floats(min_value=0.0, max_value=0.3)), 2)
    descendants = nx.descendants(graph, fault)

    scores: dict[str, NodeScore] = {}
    for nd in nodes:
        if nd == fault:
            value = fault_score
        elif nd in descendants:
            # Inherited degradation: never back above threshold (so no second
            # origin can legitimately appear downstream) and never as low as the
            # fault itself. The strict margin matters inside a CYCLE, where the
            # fault's "descendants" include its own predecessors: with an equal
            # score the engine has no evidence to prefer one over the other —
            # and the one that received healthy input is then the better answer,
            # so the generator would be asserting a ground truth it did not
            # actually establish.
            value = round(
                min(0.49, fault_score + 0.05 + draw(st.floats(0.0, 0.4))), 2
            )
        else:
            value = round(draw(st.floats(min_value=0.8, max_value=1.0)), 2)
        scores[nd] = NodeScore(
            run_id=nd, score=value, components={}, input_flawed=None,
            unscored_reason=None, judge_note=None,
        )
    tv = draw(
        st.sampled_from([None, TerminalVerdict(bad=True, score=0.2, reasoning="bad")])
    )
    return _build(nodes, edges, scores, {nd: nd for nd in nodes}, tv), fault


@SETTINGS
@given(single_injection())
def test_no_confident_wrong_answer(case) -> None:
    """The product-level promise. One node broke and the damage is visible: the
    engine either names it, or says it cannot localize. What it must never do is
    point confidently at a different node — a wrong culprit at high confidence
    costs more than no answer at all."""
    inp, fault = case
    report = find_blame(inp)

    if fault in report.culprit_run_ids:
        return
    assert report.confidence <= 0.5, (
        f"blamed {report.culprit_run_ids} at {report.confidence:.2f} while the "
        f"injected fault was {fault} (score {report.evidence.score_map[fault]})"
    )


@SETTINGS
@given(single_injection())
def test_single_injection_blames_the_fault_or_something_it_reached(case) -> None:
    """The recall companion: localization may legitimately fail (every path into
    the fault is itself degraded, so no break was OBSERVED), but whatever is
    named must at least be on the damage's path — never an unrelated branch."""
    inp, fault = case
    report = find_blame(inp)
    if fault in report.culprit_run_ids:
        return
    reachable = nx.descendants(_graph(inp), fault)
    assert all(c in reachable for c in report.culprit_run_ids), (
        f"culprits {report.culprit_run_ids} unrelated to the fault {fault}"
    )


# --- 4. the verdict does not depend on input ORDER ------------------------


@SETTINGS
@given(blame_input(), st.randoms(use_true_random=False))
def test_report_is_invariant_under_input_permutation(inp: BlameInput, rnd) -> None:
    """``nodes`` and ``edges`` are sets in meaning, lists in representation. A
    verdict that moves when the exporter emits spans in another order is not
    reproducible — and every golden fixture, cassette and cross-run comparison
    silently rests on this."""
    shuffled_nodes = list(inp.nodes)
    shuffled_edges = list(inp.edges)
    rnd.shuffle(shuffled_nodes)
    rnd.shuffle(shuffled_edges)
    other = _build(
        shuffled_nodes, shuffled_edges, inp.scores, inp.agent_names, inp.terminal_verdict
    )

    a, b = find_blame(inp), find_blame(other)
    assert a.report_type == b.report_type
    assert a.culprit_run_ids == b.culprit_run_ids
    assert a.confidence == pytest.approx(b.confidence)
    assert a.propagation_path == b.propagation_path
    assert a.downstream_cost_usd == pytest.approx(b.downstream_cost_usd)
    assert sorted(a.unscored_run_ids) == sorted(b.unscored_run_ids)
    assert a.evidence.topo_order == b.evidence.topo_order
    assert a.evidence.candidacy == b.evidence.candidacy
    assert a.evidence.candidacy_records == b.evidence.candidacy_records
    assert a.evidence.notes == b.evidence.notes
    assert a.evidence.note_records == b.evidence.note_records


# --- 5. degrading an unrelated node does not move blame -------------------


@SETTINGS
@given(blame_input(), st.data())
def test_lowering_an_unrelated_node_does_not_move_blame(inp: BlameInput, data) -> None:
    """Degrading a node that shares NO path with an existing culprit may add a
    second origin — it may not take the first one away. Blame that drifts to a
    branch nothing connects it to is the "blame the orchestrator instead of the
    node that broke" failure in its general form.
    """
    before = find_blame(inp)
    # Only the localized verdicts carry a culprit whose survival is meaningful:
    # a verification_gap's culprit list is legitimately REPLACED once a real
    # origin appears, and a fallback verdict names a suspect, not a culprit.
    assume(before.report_type in ("cut_point", "multi_culprit"))
    assume(before.culprit_run_ids)

    graph = _graph(inp)
    culprit = before.culprit_run_ids[0]
    # "Unrelated" is the CAUSAL closure, not just "no path between the two".
    # Whether an ancestor counts as a spreading cause or a transient low the
    # pipeline cured depends on ALL of its successors, so degrading a SIBLING of
    # the culprit (or of any of its ancestors) legitimately turns that shared
    # ancestor into an origin, which then shadows the culprit. Excluding the
    # sibling closure is what makes the remaining assertion mean "blame did not
    # drift for reasons that never touched this branch".
    upstream = nx.ancestors(graph, culprit) | {culprit}
    related = upstream | nx.descendants(graph, culprit)
    for anc in list(upstream):
        related |= set(graph.successors(anc))
    # Scoped to origins that qualified on their OWN observed evidence — a drop
    # past the gap from a healthy, scored predecessor.
    #
    # Localization is a precedence ladder: observed drops first, then degraded
    # boundaries that spread, then cumulative erosion chains, then — only when
    # nothing else explains the failure at all — a boundary whose damage the
    # pipeline cured. Everything below the top rung is a fallback that a
    # better-evidenced origin ANYWHERE is meant to displace ("blame the node that
    # broke, not the orchestrator"), so it carries no monotonicity promise by
    # design. Asserting one over the whole ladder would be testing against the
    # engine's intent; asserting it for measured breaks is the real guarantee.
    #
    # Read from the engine's own candidates rather than recomputed here: the
    # criterion runs on the CONDENSATION, where a cycle's score is its exit
    # member's, and any approximation over the raw graph exempts the wrong cases.
    assume(
        any(
            c.observed_drop
            for c in select_candidates(inp)
            if culprit in (c.run_id, c.scc_member)
        )
    )
    unrelated = [
        n for n in inp.nodes if n not in related and inp.scores[n].score is not None
    ]
    assume(unrelated)

    target = data.draw(st.sampled_from(sorted(unrelated)))
    scores = dict(inp.scores)
    scores[target] = NodeScore(
        run_id=target, score=0.05, components={}, input_flawed=None,
        unscored_reason=None, judge_note=None,
    )
    after = find_blame(
        _build(inp.nodes, inp.edges, scores, inp.agent_names, inp.terminal_verdict)
    )

    assert culprit in after.culprit_run_ids, (
        f"degrading unrelated {target} moved blame off {culprit}: "
        f"{before.report_type}{before.culprit_run_ids} -> "
        f"{after.report_type}{after.culprit_run_ids}"
    )
