"""Multi-culprit scenarios: parallel independent candidates (S16) and
deterministic tie-breaking of equal candidates (S22)."""

import pytest

from blame_engine import compute_confidence, find_blame, select_candidates


def test_multi_culprit_parallel_branches(mk) -> None:
    """S16: two independent bad branches -> both culprits, per-culprit penalty,
    report confidence = mean of per-culprit values."""
    inp = mk(
        nodes=["s", "a", "b", "x", "y"],
        edges=[("s", "a"), ("s", "b"), ("a", "x"), ("b", "y")],
        scores={"s": 1.0, "a": 0.2, "b": 0.3, "x": 0.4, "y": 0.9},
    )
    candidates = select_candidates(inp)
    assert [c.run_id for c in candidates] == ["a", "b"]

    report = find_blame(inp)
    assert report.report_type == "multi_culprit"
    assert report.culprit_run_ids == ["a", "b"]

    expected = sum(
        compute_confidence(c, inp.config, multi_culprit=True) for c in candidates
    ) / 2
    assert report.confidence == pytest.approx(expected)
    # a: raw 0.88 * 0.8; b: raw 0.82 * 0.8 -> mean 0.68
    assert report.confidence == pytest.approx(0.68)
    # Cost covers culprits and their descendants, not the shared ancestor s.
    assert report.downstream_cost_usd == pytest.approx(4.0)


def test_candidate_tie_break_by_end_time(mk) -> None:
    """S22: equal candidates are ordered by earlier end_time first."""
    inp = mk(
        nodes=["s", "zeta", "alpha"],
        edges=[("s", "zeta"), ("s", "alpha")],
        scores={"s": 1.0, "zeta": 0.2, "alpha": 0.2},
        end_times={"s": 0.0, "zeta": 1.0, "alpha": 2.0},
    )
    report = find_blame(inp)
    assert report.report_type == "multi_culprit"
    assert report.culprit_run_ids == ["zeta", "alpha"]  # t=1 before t=2


def test_candidate_tie_break_by_run_id(mk) -> None:
    """S22: equal end_times fall back to run_id order."""
    inp = mk(
        nodes=["s", "zeta", "alpha"],
        edges=[("s", "zeta"), ("s", "alpha")],
        scores={"s": 1.0, "zeta": 0.2, "alpha": 0.2},
        end_times={"s": 0.0, "zeta": 1.0, "alpha": 1.0},
    )
    report = find_blame(inp)
    assert report.report_type == "multi_culprit"
    assert report.culprit_run_ids == ["alpha", "zeta"]
