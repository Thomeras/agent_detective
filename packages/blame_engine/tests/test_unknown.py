"""Unknown-score scenarios: capped confidence (S12, S13), unknown is never a
culprit (S15). Missing score is None end-to-end, never defaulted to 1.0."""

import pytest

from blame_engine import NodeScore, TerminalVerdict, find_blame, select_candidates


def test_unknown_between_healthy_and_bad(mk) -> None:
    """S12: A(1.0) -> B(None) -> C(0.3) -> cut_point C, confidence <= 0.6,
    B lands in unscored_run_ids and evidence.unknown_ancestors."""
    inp = mk(nodes=["a", "b", "c"], edges=[("a", "b"), ("b", "c")],
             scores={"a": 1.0, "b": None, "c": 0.3})
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["c"]
    # All predecessors unknown -> base None -> drop None -> only severity term.
    assert report.confidence == pytest.approx(0.3 * 0.4)
    assert report.confidence <= 0.6
    assert report.unscored_run_ids == ["b"]
    assert report.evidence.unknown_ancestors == ["b"]
    assert report.evidence.score_map["b"] is None


def test_unknown_ancestor_anywhere_upstream_caps_confidence(mk) -> None:
    """S13: a GENUINELY unknown predecessor anywhere in the upstream cone caps
    confidence at unknown_confidence_cap (0.6), even hops away. 'Genuine' =
    judge_error (it produced output we could not score, so it might hide the
    origin) — as opposed to a structural root (payload_missing), which is
    excluded by design (see the next test)."""
    unknown_agent = NodeScore(
        run_id="u", score=None, components={}, input_flawed=None,
        unscored_reason="judge_error", judge_note=None,
    )
    inp = mk(
        nodes=["u", "m", "b", "c"],
        edges=[("u", "m"), ("m", "b"), ("b", "c")],
        scores={"u": unknown_agent, "m": 0.9, "b": 1.0, "c": 0.2},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["c"]
    # raw would be 0.88 (gap=1.0, severity=0.6, pred=1.0); capped at 0.6.
    assert report.confidence == pytest.approx(0.6)
    assert report.evidence.unknown_ancestors == ["u"]


def test_structural_root_ancestor_does_not_cap_confidence(mk) -> None:
    """Fix #2: a structural root (payload_missing SOURCE — an orchestrator entry
    with no output) is excluded by design as a culprit, so it must NOT suppress
    confidence as a 'hidden origin'. A directly-observed failure downstream of it
    keeps its full, honest confidence and the root is not an unknown_ancestor."""
    inp = mk(
        nodes=["start", "m", "b", "c"],
        edges=[("start", "m"), ("m", "b"), ("b", "c")],
        scores={"start": None, "m": 0.9, "b": 1.0, "c": 0.2},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["c"]
    # NOT capped: raw 0.88 stands (gap=1.0, severity=0.6, pred=1.0).
    assert report.confidence == pytest.approx(0.88)
    # The structural root is excluded from the hidden-origin set.
    assert report.evidence.unknown_ancestors == []


def test_unknown_node_is_never_culprit(mk) -> None:
    """S15: an unscored node can never be blamed."""
    # Unknown node with a bad downstream node: the downstream node takes the blame.
    inp = mk(nodes=["u", "b"], edges=[("u", "b")], scores={"u": None, "b": 0.3})
    candidates = select_candidates(inp)
    assert [c.run_id for c in candidates] == ["b"]
    report = find_blame(inp)
    assert report.culprit_run_ids == ["b"]
    assert "u" not in report.culprit_run_ids

    # A genuinely UNKNOWN agent (it produced output but the judge could not score
    # it -> judge_error) plus a bad verdict: it might be the real culprit, so we
    # stay unclassified rather than crying composition_failure. (Contrast a
    # structural root with no output at all -> payload_missing -> non-blocking.)
    unknown_agent = NodeScore(
        run_id="u", score=None, components={}, input_flawed=None,
        unscored_reason="judge_error", judge_note=None,
    )
    inp2 = mk(
        nodes=["u", "b"],
        edges=[("u", "b")],
        scores={"u": unknown_agent, "b": 0.9},
        terminal_verdict=TerminalVerdict(bad=True, score=0.1, reasoning="bad output"),
    )
    report2 = find_blame(inp2)
    assert report2.report_type == "unclassified"
    assert report2.culprit_run_ids == []
