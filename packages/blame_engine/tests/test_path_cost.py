"""S24: downstream cost (culprit + descendants, union/dedup across culprits).
S25: propagation path expands super-nodes to members ordered by end_time,
and targets the worst-scored (or flagged) terminal."""

import pytest

from blame_engine import TerminalVerdict, downstream_cost, find_blame


def test_cost_counts_culprit_and_descendants_not_siblings(mk) -> None:
    inp = mk(
        nodes=["s", "a", "b", "c"],
        edges=[("s", "a"), ("a", "b"), ("s", "c")],
        costs={"s": 10.0, "a": 2.0, "b": 3.0, "c": 100.0},
    )
    assert downstream_cost(inp, ["a"]) == pytest.approx(5.0)  # a + b
    assert downstream_cost(inp, []) == pytest.approx(0.0)


def test_cost_multi_culprit_union_dedup(mk) -> None:
    inp = mk(
        nodes=["s1", "s2", "a", "c", "x"],
        edges=[("s1", "a"), ("s2", "c"), ("a", "x"), ("c", "x")],
        costs={"s1": 100.0, "s2": 100.0, "a": 1.0, "c": 2.0, "x": 4.0},
    )
    # x is a shared descendant and must be counted once.
    assert downstream_cost(inp, ["a", "c"]) == pytest.approx(7.0)


def test_cost_inside_cycle_dedups(mk) -> None:
    inp = mk(
        nodes=["a", "b", "t"],
        edges=[("a", "b"), ("b", "a"), ("b", "t")],
        costs={"a": 1.0, "b": 2.0, "t": 4.0},
    )
    assert downstream_cost(inp, ["a"]) == pytest.approx(7.0)  # a + b + t, no dupes


def test_path_expands_scc_members_by_end_time(mk) -> None:
    """S25: members land on the path chronologically, not in id/input order."""
    inp = mk(
        nodes=["x", "c", "b", "a", "end"],  # input order deliberately scrambled
        edges=[("x", "a"), ("a", "b"), ("b", "c"), ("c", "a"), ("c", "end")],
        scores={"x": 0.1, "a": 0.9, "b": 0.8, "c": 0.9, "end": 0.9},
        end_times={"x": 0.0, "a": 1.0, "b": 2.0, "c": 3.0, "end": 4.0},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["x"]
    assert report.propagation_path == ["x", "a", "b", "c", "end"]


def test_path_targets_worst_scored_terminal(mk) -> None:
    inp = mk(
        nodes=["o", "t1", "t2"],
        edges=[("o", "t1"), ("o", "t2")],
        scores={"o": 0.1, "t1": 0.9, "t2": 0.3},
        end_times={"o": 0.0, "t1": 2.0, "t2": 1.0},
    )
    report = find_blame(inp)
    assert report.culprit_run_ids == ["o"]
    assert report.propagation_path == ["o", "t2"]  # worst-scored terminal


def test_path_targets_flagged_terminal_when_verdict_bad(mk) -> None:
    inp = mk(
        nodes=["o", "t1", "t2"],
        edges=[("o", "t1"), ("o", "t2")],
        scores={"o": 0.1, "t1": 0.9, "t2": 0.3},
        end_times={"o": 0.0, "t1": 2.0, "t2": 1.0},
        terminal_verdict=TerminalVerdict(bad=True, score=0.0, reasoning="bad"),
    )
    report = find_blame(inp)
    assert report.culprit_run_ids == ["o"]
    # Flagged terminal = last finished (t1), overriding the worst-scored one.
    assert report.propagation_path == ["o", "t1"]


def test_path_falls_back_to_unscored_terminal(mk) -> None:
    """No scored terminals and no verdict: target the last finished terminal."""
    inp = mk(
        nodes=["x", "t"],
        edges=[("x", "t")],
        scores={"x": 0.2, "t": None},
    )
    report = find_blame(inp)
    assert report.culprit_run_ids == ["x"]
    assert report.propagation_path == ["x", "t"]


def test_path_into_cyclic_sink_with_unknown_exit(mk) -> None:
    """Sink super-node is a cycle whose exit is unscored: the path still
    reaches it and expands its members chronologically."""
    inp = mk(
        nodes=["x", "a", "b"],
        edges=[("x", "a"), ("a", "b"), ("b", "a")],
        scores={"x": 0.2, "a": 0.7, "b": None},
        end_times={"x": 0.0, "a": 1.0, "b": 2.0},  # b is the exit, unscored
    )
    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["x"]
    assert report.propagation_path == ["x", "a", "b"]


def test_cost_ignores_unknown_culprit_ids(mk) -> None:
    inp = mk(nodes=["a"], costs={"a": 2.0})
    assert downstream_cost(inp, ["ghost"]) == pytest.approx(0.0)
