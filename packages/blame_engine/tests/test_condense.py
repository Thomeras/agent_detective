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


def test_scc_bad_iteration_healthy_exit_stays_healthy(mk) -> None:
    """S4a: bad middle iteration, healthy exit -> super-node healthy,
    min_member_score kept as evidence only."""
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
    assert report.culprit_run_ids == []
    assert report.evidence.score_map["b"] == pytest.approx(0.1)
    assert report.report_type != "loop_detected"


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
    raw = 0.88  # gap=1.0, severity=0.6, pred=1.0 -> 0.5 + 0.18 + 0.2
    assert report.confidence == pytest.approx(raw * 0.8)
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
