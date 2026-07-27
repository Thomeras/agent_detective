"""Classification rows 2, 6, 7: composition failure (S17, S20b),
root_cause_external (S18), healthy sampled graph (S19)."""

import pytest

from blame_engine import NodeScore, TerminalVerdict, find_blame
from conftest import note_of

BAD = TerminalVerdict(bad=True, score=0.1, reasoning="terminal output is wrong")


def test_composition_failure_blames_source(mk) -> None:
    """S17: all scores >= threshold, no significant drops, terminal bad ->
    composition_failure with the source/orchestrator as culprit."""
    inp = mk(
        nodes=["o", "a", "b"],
        edges=[("o", "a"), ("a", "b")],
        scores={"o": 0.9, "a": 0.85, "b": 0.8},
        terminal_verdict=BAD,
    )
    report = find_blame(inp)

    assert report.report_type == "composition_failure"
    assert report.culprit_run_ids == ["o"]
    assert report.propagation_path == ["o", "a", "b"]
    # Fallback verdict: capped so the UI never claims certainty on a guess.
    assert report.confidence == pytest.approx(0.4)
    assert report.downstream_cost_usd == pytest.approx(3.0)


def test_single_node_good_score_bad_verdict_is_composition_failure(mk) -> None:
    """S20b: single-node graph, healthy score, bad terminal verdict."""
    inp = mk(nodes=["x"], scores={"x": 0.9}, terminal_verdict=BAD)
    report = find_blame(inp)

    assert report.report_type == "composition_failure"
    assert report.culprit_run_ids == ["x"]
    assert report.propagation_path == ["x"]


def test_source_candidate_with_flawed_input_is_external(mk) -> None:
    """S18: exactly one candidate, it is a source and input_flawed=True ->
    root_cause_external."""
    flawed = NodeScore(
        run_id="s", score=0.3, components={}, input_flawed=True,
        unscored_reason=None, judge_note="input was already garbage",
    )
    inp = mk(nodes=["s", "t"], edges=[("s", "t")], scores={"s": flawed, "t": 0.9})
    report = find_blame(inp)

    assert report.report_type == "root_cause_external"
    assert report.culprit_run_ids == ["s"]
    # Confidence formula yields 0.82, but root_cause_external is capped at 0.5:
    # the fault originated outside the observed graph, so we cannot be more sure
    # than "the input was already bad".
    assert report.confidence == pytest.approx(0.5)
    assert report.evidence.judge_notes == {"s": "input was already garbage"}


def test_source_candidate_without_flawed_input_stays_cut_point(mk) -> None:
    """S18 complement: input_flawed=False keeps the plain cut_point row."""
    clean = NodeScore(
        run_id="s", score=0.3, components={}, input_flawed=False,
        unscored_reason=None, judge_note=None,
    )
    inp = mk(nodes=["s", "t"], edges=[("s", "t")], scores={"s": clean, "t": 0.9})
    assert find_blame(inp).report_type == "cut_point"


def test_healthy_sampled_graph_is_unclassified(mk) -> None:
    """S19: sampled graph with healthy scores and no verdict -> unclassified,
    no incident material."""
    inp = mk(nodes=["a", "b"], edges=[("a", "b")], scores={"a": 0.9, "b": 0.95})
    report = find_blame(inp)

    assert report.report_type == "unclassified"
    assert report.culprit_run_ids == []
    assert report.propagation_path == []
    assert report.confidence == 0.0
    assert report.downstream_cost_usd == 0.0
    # The rationale is present AND typed: an "unclassified" verdict must say
    # which precondition ruled every other verdict out.
    assert [r["code"] for r in note_of(report, "unclassified")["reasons"]]
