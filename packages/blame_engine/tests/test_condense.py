"""Condensation scenarios: benign loops (S1), SCC scoring (S4), self-loop and
whole-graph SCC (S5)."""

import pytest

from blame_engine import condense, find_blame


def test_benign_retry_loop_condenses_and_blame_runs(mk) -> None:
    """S1: 3-iteration retry cycle with healthy scores condenses fine, no
    loop_detected, blame proceeds normally."""
    inp = mk(
        nodes=["a", "b", "c", "t"],
        edges=[("a", "b"), ("b", "c"), ("c", "a"), ("c", "t")],
        scores={"a": 0.9, "b": 0.85, "c": 0.9, "t": 0.9},
    )
    cond = condense(inp)
    loop_sn = next(sn for sn in cond.super_nodes.values() if sn.iterations == 3)
    assert loop_sn.exit_node == "c"  # last finished member
    assert loop_sn.score == pytest.approx(0.9)
    assert loop_sn.min_member_score == pytest.approx(0.85)

    report = find_blame(inp)
    assert report.report_type != "loop_detected"
    assert report.culprit_run_ids == []
    assert report.evidence.loop_anomalies == []


def test_scc_bad_iteration_healthy_exit_is_localized_inside_the_cycle(mk) -> None:
    """S4a: bad middle iteration, healthy exit.

    The super-node's SCORE stays the exit's — that is the value which flows
    downstream — but blame is no longer blind to what happened inside. This
    assertion used to read ``culprit_run_ids == []``: the cycle read healthy, and
    a member sitting at 0.10 in the very same score map was named by nothing.
    That is the shape an orchestrator with sub-agents takes (SPAWN out,
    TOOL_DELEGATION back, orchestrator ends last and IS the exit), so the blind
    spot covered the most common multi-agent topology there is.
    """
    inp = mk(
        nodes=["a", "b", "c", "t"],
        edges=[("a", "b"), ("b", "c"), ("c", "a"), ("c", "t")],
        scores={"a": 0.9, "b": 0.1, "c": 0.9, "t": 0.9},
    )
    cond = condense(inp)
    loop_sn = next(sn for sn in cond.super_nodes.values() if sn.iterations == 3)
    assert loop_sn.score == pytest.approx(0.9)
    assert loop_sn.min_member_score == pytest.approx(0.1)

    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["b"]
    assert report.evidence.score_map["b"] == pytest.approx(0.1)
    # Measured against b's REAL in-cycle predecessor (a 0.90), not the exit's.
    assert report.evidence.drops["b"] == pytest.approx(0.8)
    assert report.report_type != "loop_detected"


def test_scc_bad_iteration_that_the_loop_repaired_is_a_near_miss(mk) -> None:
    """The same cycle with terminal ground truth that the deliverable is fine: a
    retry loop whose later iteration came out healthy did its job. Localizing
    inside cycles must not turn every successful retry into a cut_point."""
    from blame_engine import TerminalVerdict

    inp = mk(
        nodes=["a", "b", "c", "t"],
        edges=[("a", "b"), ("b", "c"), ("c", "a"), ("c", "t")],
        scores={"a": 0.9, "b": 0.1, "c": 0.9, "t": 0.9},
        terminal_verdict=TerminalVerdict(
            bad=False, score=0.95, reasoning="deliverable meets the goal"
        ),
    )
    report = find_blame(inp)

    assert report.report_type == "degraded_recovered"
    assert report.culprit_run_ids == ["b"]


def test_scc_bad_exit_is_culprit_with_penalty(mk) -> None:
    """S4b: bad exit node -> the SCC itself is the culprit, confidence
    multiplied by scc_confidence_penalty (0.8)."""
    inp = mk(
        nodes=["a", "b", "c", "t"],
        edges=[("a", "b"), ("b", "c"), ("c", "a"), ("c", "t")],
        scores={"a": 0.9, "b": 0.8, "c": 0.2, "t": 0.9},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["c"]  # exit node represents the SCC
    # 0.5*gap(0.6/0.5 -> 1.0) + 0.3*sev(0.6) + 0.2*pred((0.8-0.5)/0.5) = 0.80,
    # times the SCC penalty 0.8 = 0.64. The observability-boundary cap does NOT
    # apply: c has a real scored predecessor INSIDE the cycle (b 0.80), so the
    # baseline is measured. It previously read 0.6 because the drop was taken
    # from b while the predecessor term was taken from a fictional 1.00 source
    # baseline — one number built from two incompatible stories.
    assert report.confidence == pytest.approx(0.64)
    # SCC expands chronologically on the propagation path.
    assert report.propagation_path == ["a", "b", "c", "t"]


def test_self_loop_does_not_crash(mk) -> None:
    """S5a: a self-loop is condensed like any cycle; healthy scores -> nothing."""
    inp = mk(nodes=["x", "t"], edges=[("x", "x"), ("x", "t")], scores={"x": 0.9, "t": 0.9})
    cond = condense(inp)
    x_sn = cond.super_nodes[cond.node_to_super["x"]]
    assert x_sn.iterations == 1
    assert x_sn.has_self_loop

    report = find_blame(inp)
    assert report.report_type != "loop_detected"
    assert report.culprit_run_ids == []


def test_whole_graph_single_scc(mk) -> None:
    """S5b: the whole graph is one big SCC -> one super-node, blame still runs."""
    inp = mk(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")],
        scores={"a": 0.9, "b": 0.85, "c": 0.95, "d": 0.9},
    )
    cond = condense(inp)
    assert len(cond.super_nodes) == 1
    sn = next(iter(cond.super_nodes.values()))
    assert sn.iterations == 4
    assert sn.exit_node == "d"

    report = find_blame(inp)
    assert report.report_type != "loop_detected"
    assert report.culprit_run_ids == []
