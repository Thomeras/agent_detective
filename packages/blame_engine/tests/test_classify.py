"""Scenario 14: every score unknown -> unclassified ("no_scores")."""

from blame_engine import find_blame


def test_all_unknown_scores_unclassified(mk) -> None:
    inp = mk(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c")])
    report = find_blame(inp)

    assert report.report_type == "unclassified"
    assert report.culprit_run_ids == []
    assert report.confidence == 0.0
    assert sorted(report.unscored_run_ids) == ["a", "b", "c"]
    assert any("no_scores" in note for note in report.evidence.notes)
    assert report.evidence.score_map == {"a": None, "b": None, "c": None}
    assert report.downstream_cost_usd == 0.0
