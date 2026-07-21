"""Cut-point scenarios: source baseline (S10) and single-node graph (S20a)."""

import pytest

from blame_engine import find_blame


def test_source_below_threshold_is_cut_point(mk) -> None:
    """S10: source scores 0.4 against the 1.0 source baseline -> source is the cut point."""
    inp = mk(nodes=["s", "t"], edges=[("s", "t")], scores={"s": 0.4, "t": 0.4})
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["s"]
    # "t" inherited the low quality (drop 0.0 from base 0.4) and is not a candidate.
    assert report.evidence.drops == {"s": pytest.approx(0.6)}
    # gap=1.0 (0.6/0.5 clamped), severity=0.2, pred=1.0 -> 0.5 + 0.06 + 0.2
    assert report.confidence == pytest.approx(0.76)
    assert report.propagation_path == ["s", "t"]
    assert report.downstream_cost_usd == pytest.approx(2.0)


def test_single_node_bad_score_is_cut_point(mk) -> None:
    """S20a: single-node graph with a bad score -> cut_point on itself."""
    inp = mk(nodes=["x"], scores={"x": 0.2})
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["x"]
    assert report.propagation_path == ["x"]
    assert report.downstream_cost_usd == pytest.approx(1.0)
    # gap=1.0 (0.8/0.5 clamped), severity=0.6, pred=1.0 -> 0.5 + 0.18 + 0.2
    assert report.confidence == pytest.approx(0.88)


def test_gradient_degradation_blames_gap_origin(mk) -> None:
    """S8: 1.0 -> 0.55 -> 0.45 with threshold 0.5 -> culprit is 0.55, NOT 0.45."""
    from blame_engine import select_candidates

    inp = mk(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c")],
             scores={"a": 1.0, "b": 0.55, "c": 0.45})
    candidates = select_candidates(inp)
    assert [c.run_id for c in candidates] == ["b"]

    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["b"]
    # c (0.45) is shadowed by b; only the origin's drop is reported.
    assert report.evidence.drops == {"b": pytest.approx(0.45)}


def test_below_threshold_small_drop_is_not_candidate(mk) -> None:
    """S9: below threshold but drop < min_drop from a broken predecessor ->
    inherited degradation, not a candidate."""
    from blame_engine import select_candidates

    inp = mk(nodes=["a", "b"], edges=[("a", "b")], scores={"a": 0.3, "b": 0.25})
    candidates = select_candidates(inp)
    assert [c.run_id for c in candidates] == ["a"]

    report = find_blame(inp)
    assert report.culprit_run_ids == ["a"]
    assert report.evidence.drops == {"a": pytest.approx(0.7)}


def test_shadowing_drops_downstream_candidate(mk) -> None:
    """S11: a candidate with another candidate among its ancestors is dropped."""
    from blame_engine import select_candidates

    inp = mk(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c")],
             scores={"a": 1.0, "b": 0.3, "c": 0.2})
    candidates = select_candidates(inp)
    # c (drop 0.1, below threshold) would be a candidate but is shadowed by b.
    assert [c.run_id for c in candidates] == ["b"]
    assert find_blame(inp).culprit_run_ids == ["b"]
