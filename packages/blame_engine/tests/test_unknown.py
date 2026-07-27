"""Unknown-score scenarios: capped confidence (S12, S13), unknown is never a
culprit (S15). Missing score is None end-to-end, never defaulted to 1.0.

The last group covers the state a ``--no-judge`` run produces — EVERY score
unknown — where "we measured nothing" and "we measured something that was not a
score" have to stay distinguishable."""

import pytest

from conftest import candidacy_of, note_slugs, verdict_of

from blame_engine import NodeScore, TerminalVerdict, find_blame, select_candidates


def _unscored_with_breach(run_id: str, *, contract=()) -> NodeScore:
    """A node the judge never scored, carrying deterministic evidence only.

    This is what tier2 records for every node of a ``--no-judge`` run (or of any
    run whose composite never clears SCORE_MIN_WEIGHT): score None, reason
    ``insufficient_components``, with the hard checks still populated."""
    return NodeScore(
        run_id=run_id,
        score=None,
        components={"schema": None, "judge": None, "heuristics": None},
        input_flawed=None,
        unscored_reason="insufficient_components",
        judge_note=None,
        contract_violations=tuple(contract),
    )


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


# --- every score unknown: measured-nothing vs measured-something-else -----


def test_every_score_unknown_with_a_contract_breach_still_localizes(mk) -> None:
    """The `no_scores` short-circuit must not discard deterministic evidence.

    ``find_blame``'s first cascade row was ``all(s is None ...)`` -> note
    ``no_scores``, and it ran BEFORE the candidate list. A run with the judge off
    therefore reported `unclassified` with zero culprits and confidence 0.0 even
    though a hard check had OBSERVED a carried parameter arrive intact and leave
    rewritten at a named node — the CLI printed "nothing could be measured" over
    a measurement. "No quality score" is not "no evidence"; the deterministic
    channel exists to say so.
    """
    inp = mk(
        nodes=["root", "a", "b"],
        edges=[("root", "a"), ("a", "b")],
        scores={
            "root": None,  # payload-less orchestrator: a structural root
            "a": _unscored_with_breach("a", contract=[("file_type", "docx", "md")]),
            "b": _unscored_with_breach("b"),
        },
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["a"]
    # Deterministic observation: the input/output diff saw origination, so both
    # halves of the confidence pair are the near-certain 0.95 — and the headline
    # is not diluted by the absence of a judged score.
    assert report.confidence == pytest.approx(0.95)
    assert report.evidence.observation_confidence == pytest.approx(0.95)
    assert report.evidence.attribution_confidence == pytest.approx(0.95)
    assert "no_scores" not in note_slugs(report)

    (defect,) = report.evidence.defects
    assert defect["kind"] == "contract"
    assert defect["channel"] == "deterministic"
    # The caveat fields carry what was NOT measured, so the verdict cannot be
    # read as "content was checked and cleared".
    assert defect["quality_unmeasured"] is True
    assert defect["unverified_in_channel"] == "content"
    assert defect["recovered"] is False


def test_an_unscored_deterministic_origin_is_never_given_a_score(mk) -> None:
    """The stand-in 0.0 this fix had to remove.

    A deterministic candidate on an unjudged node used to be built with
    ``score=0.0``, which the report then rendered as "judged 0.00" — an assertion
    that the judge scored the node terrible when it never ran, and a severity term
    that manufactured ~0.3 of attribution out of it. Absence stays absent."""
    inp = mk(
        nodes=["root", "a", "b"],
        edges=[("root", "a"), ("a", "b")],
        scores={
            "root": None,
            "a": _unscored_with_breach("a", contract=[("file_type", "docx", "md")]),
            "b": _unscored_with_breach("b"),
        },
    )
    (candidate,) = select_candidates(inp)
    assert candidate.run_id == "a"
    assert candidate.score is None

    report = find_blame(inp)
    assert report.evidence.score_map["a"] is None
    # The candidacy line states the deterministic basis (not "unscored — never a
    # candidate", which would contradict the headline) and quotes no score.
    assert verdict_of(report, "a") == "origin_deterministic"
    assert candidacy_of(report, "a")["score"] is None


def test_every_score_unknown_and_nothing_else_measured_stays_unclassified(mk) -> None:
    """The other direction, and the reason the row still exists: with no judged
    score AND no deterministic evidence anywhere, the honest answer is still "we
    could not measure this run". Absence of evidence may not become a verdict."""
    inp = mk(
        nodes=["root", "a", "b"],
        edges=[("root", "a"), ("a", "b")],
        scores={
            "root": None,
            "a": _unscored_with_breach("a"),
            "b": _unscored_with_breach("b"),
        },
    )
    report = find_blame(inp)

    assert report.report_type == "unclassified"
    assert report.culprit_run_ids == []
    assert report.confidence == 0.0
    assert "no_scores" in note_slugs(report)


def test_every_score_unknown_with_a_loop_breach_still_reports_the_loop(mk) -> None:
    """A loop-limit breach is deterministic evidence too — it is counted, not
    judged. It was discarded by the same short-circuit: a 12-iteration runaway
    with the judge off reported `unclassified` while the anomaly sat in the
    evidence, so the incident could only ever come from tier1's flag."""
    nodes = [f"l{i}" for i in range(12)] + ["t"]
    edges = [(f"l{i}", f"l{(i + 1) % 12}") for i in range(12)] + [("l11", "t")]
    inp = mk(nodes=nodes, edges=edges, scores={n: None for n in nodes})
    report = find_blame(inp)

    assert report.report_type == "loop_detected"
    assert report.culprit_run_ids == [f"l{i}" for i in range(12)]
    assert report.evidence.loop_anomalies[0].limit_kind == "max_iterations"
    assert "no_scores" not in note_slugs(report)
