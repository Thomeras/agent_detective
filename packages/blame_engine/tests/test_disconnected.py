"""S21: disconnected graphs — blame and propagation path stay within the
culprit's component; breaks in both components yield multi_culprit."""

import pytest

from blame_engine import find_blame


def test_break_in_one_component(mk) -> None:
    inp = mk(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("c", "d")],
        scores={"a": 1.0, "b": 0.2, "c": 1.0, "d": 0.9},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["b"]
    assert report.propagation_path == ["b"]  # b is the terminal of its component
    assert report.downstream_cost_usd == pytest.approx(1.0)


def test_breaks_in_both_components_are_multi_culprit(mk) -> None:
    inp = mk(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("c", "d")],
        scores={"a": 1.0, "b": 0.2, "c": 1.0, "d": 0.1},
    )
    report = find_blame(inp)

    assert report.report_type == "multi_culprit"
    assert report.culprit_run_ids == ["b", "d"]
    # The globally worst terminal (d) is unreachable from the first culprit;
    # the path stays within b's component.
    assert report.propagation_path == ["b"]
