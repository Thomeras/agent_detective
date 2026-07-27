"""Cut-point scenarios: source baseline (S10) and single-node graph (S20a)."""

import pytest

from blame_engine import find_blame
from conftest import note_of


def test_source_below_threshold_is_cut_point(mk) -> None:
    """S10: source scores 0.4 against the 1.0 source baseline -> source is the cut point."""
    inp = mk(nodes=["s", "t"], edges=[("s", "t")], scores={"s": 0.4, "t": 0.4})
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["s"]
    # A source has NO measured predecessor: its 1.0 baseline is assumed, so no
    # drop is fabricated into the evidence ("-0.60 from best-scored predecessor"
    # against a fiction). The assumption still informs confidence and is declared
    # in the notes/candidacy instead.
    assert report.evidence.drops == {}
    assert note_of(report, "cut_point")["variant"] == "base_assumed"
    # Raw formula would give 0.76, but the origin sits at the OBSERVABILITY
    # BOUNDARY (assumed baseline, no contract evidence) -> hard cap 0.6.
    assert report.confidence == pytest.approx(0.6)
    assert note_of(report, "attribution_capped") is not None
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
    # Raw formula would give 0.88, but a single-node graph is ALL boundary:
    # no predecessor was ever measured -> attribution hard-capped at 0.6.
    assert report.confidence == pytest.approx(0.6)


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
    # "a" is a source: no measured predecessor, so no fabricated drop against the
    # assumed 1.0 baseline appears in the evidence.
    assert report.evidence.drops == {}


def test_shadowing_drops_downstream_candidate(mk) -> None:
    """S11: a candidate with another candidate among its ancestors is dropped."""
    from blame_engine import select_candidates

    inp = mk(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c")],
             scores={"a": 1.0, "b": 0.3, "c": 0.2})
    candidates = select_candidates(inp)
    # c (drop 0.1, below threshold) would be a candidate but is shadowed by b.
    assert [c.run_id for c in candidates] == ["b"]
    assert find_blame(inp).culprit_run_ids == ["b"]
