"""Regression fixtures for the docx-proposal incident review.

The real run: start -> think(0.89) -> act(0.71) -> render(0.56) -> qa(0.92)
-> eval(0.93), terminal verdict BAD. The engine reported composition_failure
("no significant drops", "nobody broke") with empty verification gaps — three
distinct failures at once:

1. it discarded a real cumulative degradation signal (0.89 -> 0.71 -> 0.56),
2. it did not flag qa/eval although a bad artifact passed both verifiers,
3. it cited no evidence for "terminal verdict is bad".
"""

import pytest

from blame_engine import NodeScore, TerminalVerdict, find_blame

BAD = TerminalVerdict(
    bad=True,
    score=0.2,
    reasoning="proposal lacks the specific items and prices the input required",
)


def _sc(rid, s):
    return NodeScore(
        run_id=rid, score=s, components={}, input_flawed=None,
        unscored_reason=None, judge_note=None,
    )


def _incident(mk, **overrides):
    root = NodeScore(run_id="start", score=None, components={}, input_flawed=None,
                     unscored_reason="payload_missing", judge_note=None)
    nodes = ["start", "think", "act", "render", "qa", "eval"]
    edges = [("start", "think"), ("think", "act"), ("act", "render"),
             ("render", "qa"), ("qa", "eval")]
    scores = {
        "start": root, "think": _sc("think", 0.89), "act": _sc("act", 0.71),
        "render": _sc("render", 0.56), "qa": _sc("qa", 0.92),
        "eval": _sc("eval", 0.93),
    }
    kwargs = dict(nodes=nodes, edges=edges, scores=scores, terminal_verdict=BAD)
    kwargs.update(overrides)
    return mk(**kwargs)


def test_cumulative_degradation_is_an_origin_not_composition_failure(mk):
    """0.89 -> 0.71 -> 0.56 has no single significant drop, but the cumulative
    decline (0.33 over 2 consecutive edges) is a localisable origin signal.
    'No significant drops' was true only against the single-edge threshold."""
    report = find_blame(_incident(mk))

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["act"]  # first eroding node
    chains = report.evidence.degradation_paths
    assert len(chains) == 1
    assert chains[0]["path"] == ["think", "act", "render"]
    assert chains[0]["cumulative_drop"] == pytest.approx(0.33)
    assert any("cumulative" in n for n in report.evidence.notes)


def test_bad_terminal_with_passing_verifiers_flags_them_retroactively(mk):
    """The artifact was bad and both qa and eval let it pass with healthy
    scores — by definition a verification gap, no matter how plausible their
    verdicts read. The engine must not go blind when the judges do."""
    report = find_blame(_incident(mk))

    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {
        "qa": "passed_bad_terminal",
        "eval": "passed_bad_terminal",
    }


def test_no_origin_and_passing_verifiers_becomes_verification_gap(mk):
    """Without the degradation chain (all producers healthy), the same graph
    must classify as verification_gap — not composition_failure with 'nobody
    broke' — because the verifiers demonstrably passed a bad artifact."""
    scores = {
        "start": NodeScore(run_id="start", score=None, components={},
                           input_flawed=None, unscored_reason="payload_missing",
                           judge_note=None),
        "think": _sc("think", 0.9), "act": _sc("act", 0.88),
        "render": _sc("render", 0.85), "qa": _sc("qa", 0.92),
        "eval": _sc("eval", 0.93),
    }
    report = find_blame(_incident(mk, scores=scores))

    assert report.report_type == "verification_gap"
    assert set(report.culprit_run_ids) == {"qa", "eval"}
    assert report.confidence <= 0.6
    # Culprits of a verification_gap report are the gap verifiers — candidacy
    # must say so, not fall through to a (numerically false) origin label.
    assert "verification gap" in report.evidence.candidacy["qa"]
    assert "verification gap" in report.evidence.candidacy["eval"]


NOT_CHECKABLE = TerminalVerdict(
    bad=True,  # the LLM said "bad/empty" — but it never saw the deliverable
    score=0.0,
    reasoning="the final output is completely empty",
    checkable=False,
)


def test_not_checkable_terminal_does_not_manufacture_a_culprit(mk):
    """The docx phantom: a retry loopback removed the graph's sink, the terminal
    judge was handed the empty root wrapper and hallucinated 'completely empty'.
    A not-checkable verdict is NOT ground truth — it must not fire the
    fabrication cascade, composition_failure, or a passed_bad_terminal gap. All
    producers healthy + a self-critical 'think' flag must NOT pin think."""
    think = NodeScore(
        run_id="think", score=0.56, components={}, input_flawed=None,
        unscored_reason=None, judge_note=None,
        flags=("missing_required_content",),
    )
    report = find_blame(
        _incident(mk, terminal_verdict=NOT_CHECKABLE,
                  scores={
                      "start": NodeScore(run_id="start", score=None, components={},
                                         input_flawed=None,
                                         unscored_reason="payload_missing",
                                         judge_note=None),
                      "think": think, "act": _sc("act", 0.93),
                      "render": _sc("render", 0.93), "qa": _sc("qa", 0.92),
                      "eval": _sc("eval", 0.93),
                  })
    )

    assert report.report_type != "cut_point"           # no fabrication cascade
    assert "think" not in report.culprit_run_ids        # innocent, not pinned
    assert report.evidence.verification_gaps == []      # no phantom gap
    assert any("terminal_not_checkable" in n for n in report.evidence.notes)
    assert report.evidence.terminal_verdict["checkable"] is False


def test_terminal_verdict_evidence_is_cited(mk):
    """A report leaning on 'terminal is bad' must show the evidence: the tier1
    verdict itself plus the score/verdict conflict with healthy sinks."""
    report = find_blame(_incident(mk))

    tv = report.evidence.terminal_verdict
    assert tv == {
        "bad": True,
        "score": 0.2,
        "reasoning": BAD.reasoning,
        "checkable": True,
        "stale": False,
    }
    assert any("verdict_conflict" in n for n in report.evidence.notes)
    assert any(BAD.reasoning in n for n in report.evidence.notes)


def test_verifiers_passing_a_bad_artifact_are_gaps_even_beside_a_cut_point(mk):
    """A localised origin does not absolve downstream verifiers: they still
    passed the bad work, so the gaps stay in evidence (report type unchanged)."""
    report = find_blame(_incident(mk))

    assert report.report_type == "cut_point"
    assert {g["run_id"] for g in report.evidence.verification_gaps} == {"qa", "eval"}


def test_verifier_upstream_of_the_break_is_not_flagged(mk):
    """A verifier that saw only pre-break (good) work issued a correct PASS."""
    nodes = ["gen", "review", "transform", "out"]
    edges = [("gen", "review"), ("review", "transform"), ("transform", "out")]
    scores = {"gen": _sc("gen", 0.9), "review": _sc("review", 0.9),
              "transform": _sc("transform", 0.2), "out": _sc("out", 0.25)}
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))

    assert report.culprit_run_ids == ["transform"]
    assert report.evidence.verification_gaps == []


def test_manifestation_is_the_artifact_producer_not_the_verifier_sink(mk):
    """'Manifested at eval' was misleading: eval issued a (wrong) verdict; the
    failure surfaced in the artifact render produced."""
    report = find_blame(_incident(mk))
    assert report.evidence.manifestation_run_ids == ["render"]


def test_score_map_and_candidacy_follow_topo_order(mk):
    report = find_blame(_incident(mk))
    assert report.evidence.topo_order == [
        "start", "think", "act", "render", "qa", "eval",
    ]
    assert list(report.evidence.score_map) == report.evidence.topo_order
    assert list(report.evidence.candidacy) == report.evidence.topo_order
    assert report.evidence.verifier_run_ids == ["qa", "eval"]


def test_candidacy_carries_the_numbers_behind_each_decision(mk):
    """The trace must be auditable: score vs threshold, drop vs reference,
    exclusion reason — not bare labels."""
    report = find_blame(_incident(mk))
    c = report.evidence.candidacy

    assert "structural root" in c["start"]
    assert "0.89" in c["think"] and "degradation-path start" in c["think"]
    assert c["act"].startswith("origin")
    assert "0.33" in c["act"]
    assert "degradation path" in c["render"] and "0.56" in c["render"]
    assert "verification gap" in c["qa"] and "0.92" in c["qa"]


def test_small_drifts_still_classify_as_composition_failure(mk):
    """Guard: tiny declines (cumulative < threshold) must NOT be promoted to a
    degradation path — the composition_failure fallback remains reachable."""
    inp = mk(
        nodes=["o", "a", "b"],
        edges=[("o", "a"), ("a", "b")],
        scores={"o": 0.9, "a": 0.85, "b": 0.8},
        terminal_verdict=BAD,
    )
    report = find_blame(inp)
    assert report.report_type == "composition_failure"
    assert report.evidence.degradation_paths == []
    # And the fallback suspect is labelled as a layer suspect, not "never a
    # culprit" — the old contradiction.
    assert "suspect" in report.evidence.candidacy["o"]


def _flagged(rid, s, flags):
    return NodeScore(
        run_id=rid, score=s, components={}, input_flawed=None,
        unscored_reason=None, judge_note=None, flags=tuple(flags),
    )


def test_fabrication_cascade_blames_the_honest_underdeliverer(mk):
    """think's own judge admitted missing required content (flag) while every
    downstream node claimed success and the terminal says 'no content'. The
    first node where reality diverged from claims is the origin — not the
    verifiers, not the orchestrator."""
    nodes = ["think", "act", "render", "qa", "eval"]
    edges = [("think", "act"), ("act", "render"), ("render", "qa"), ("qa", "eval")]
    scores = {
        "think": _flagged("think", 0.67, ["missing_required_content"]),
        "act": _sc("act", 0.71), "render": _sc("render", 0.93),
        "qa": _sc("qa", 0.93), "eval": _sc("eval", 0.93),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["think"]
    assert report.confidence == pytest.approx(0.65)
    assert any("fabrication cascade" in n for n in report.evidence.notes)
    assert "fabrication cascade" in report.evidence.candidacy["think"]
    # Verifiers that passed the bad work stay flagged in evidence.
    assert {g["run_id"] for g in report.evidence.verification_gaps} == {"qa", "eval"}
    # The producer's healthy score is confronted, not left standing as fact.
    assert any("claims_vs_reality" in n for n in report.evidence.notes)
    assert "claims-vs-reality" in report.evidence.candidacy["render"]


def test_engine_leaves_hypotheses_empty_settled_origin_by_default(mk):
    """The competing-origins breakdown is a tier2 concern (it needs fact
    propagation and the degenerate-output flag, which live at tier2). The engine
    itself never fabricates rival hypotheses: with a hard cumulative-degradation
    origin it emits one settled origin and an EMPTY hypotheses list — a single
    confident origin is honest here because nothing in-graph contests it."""
    report = find_blame(_incident(mk))
    assert report.report_type == "cut_point"
    assert report.evidence.hypotheses == []


def test_fabrication_flag_without_bad_terminal_changes_nothing(mk):
    """A content flag alone (terminal ok) must not invent a culprit."""
    nodes = ["think", "act"]
    edges = [("think", "act")]
    scores = {
        "think": _flagged("think", 0.67, ["missing_required_content"]),
        "act": _sc("act", 0.9),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores))
    assert report.report_type == "unclassified"
    assert report.culprit_run_ids == []


def test_unclassified_explains_which_preconditions_failed(mk):
    """A negative conclusion needs a trace: say WHY nothing matched."""
    inp = mk(nodes=["a", "b"], edges=[("a", "b")], scores={"a": 0.9, "b": 0.95})
    report = find_blame(inp)
    note = next(n for n in report.evidence.notes if n.startswith("unclassified"))
    assert "no terminal verdict available" in note


def test_candidacy_never_misstates_a_comparison(mk):
    """Regression for the 'origin — score 0.93 < threshold 0.50' lie: every
    numeric claim in the candidacy trace must be true for its node."""
    report = find_blame(_incident(mk))
    for run_id, text in report.evidence.candidacy.items():
        if "< threshold" in text:
            score = report.evidence.score_map[run_id]
            assert score is not None and score < 0.5, (run_id, text)


def test_structural_root_labelled_by_design_no_warning(mk):
    """start has no payload BY DESIGN — candidacy says so, and there is no
    instrumentation warning for it. A non-root payload gap does warn."""
    report = find_blame(_incident(mk))
    assert "structural root" in report.evidence.candidacy["start"]
    assert not any("instrumentation_warning" in n for n in report.evidence.notes)

    broken = NodeScore(run_id="mid", score=None, components={}, input_flawed=None,
                       unscored_reason="payload_missing", judge_note=None)
    inp = mk(nodes=["a", "mid", "b"], edges=[("a", "mid"), ("mid", "b")],
             scores={"a": 0.9, "mid": broken, "b": 0.9})
    report2 = find_blame(inp)
    assert any("instrumentation_warning" in n for n in report2.evidence.notes)


def test_whistle_blowing_verifier_is_not_a_retroactive_gap(mk):
    """A verifier that issued FAIL on the bad work blew the whistle — flagging
    it 'passed_bad_terminal' would be a false accusation (real-run regression:
    eval correctly FAILed the doc and was still listed as a gap)."""
    nodes = ["gen", "qa", "eval"]
    edges = [("gen", "qa"), ("qa", "eval")]
    scores = {
        "gen": _flagged("gen", 0.67, ["missing_required_content"]),
        "qa": _flagged("qa", 0.95, ["issued_pass"]),
        "eval": _flagged("eval", 0.93, ["issued_fail"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))

    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {"qa": "passed_bad_terminal"}  # eval exonerated
    assert "whistle-blower" in report.evidence.candidacy["eval"]


def test_cascade_participants_are_labelled_and_rubber_stamp_score_overridden(mk):
    """Downstream producers that claimed success over flagged-missing input are
    cascade participants, not 'healthy'; a verifier PASS refuted by the terminal
    gets its 'verdict correctness' score overridden (shown, not rewritten)."""
    nodes = ["think", "act", "render", "qa", "eval"]
    edges = [("think", "act"), ("act", "render"), ("render", "qa"), ("qa", "eval")]
    scores = {
        "think": _flagged("think", 0.67, ["missing_required_content"]),
        "act": _sc("act", 0.93), "render": _sc("render", 0.93),
        "qa": _flagged("qa", 1.0, ["issued_pass"]),
        "eval": _flagged("eval", 0.93, ["issued_fail"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))

    c = report.evidence.candidacy
    assert "fabrication-cascade participant" in c["act"]
    # render is the manifestation producer, so the stronger claims-vs-reality
    # label wins there — either way its score reads as an unverified claim.
    assert "unverified claim" in c["render"]
    assert any("cascade_participants" in n for n in report.evidence.notes)

    overrides = {o["run_id"]: o for o in report.evidence.score_overrides}
    assert set(overrides) == {"qa"}  # eval blew the whistle — no override
    assert overrides["qa"]["original"] == 1.0
    assert overrides["qa"]["effective"] == 0.1


# --- verdict_scored_incorrect cross-checked against terminal ground truth -----
# The cybersecurity-proposal incident: a clean generative_simon run rendered a
# full, good proposal. The TERMINAL judge saw the real document and returned
# verdict=ok, score=1.0 ("comprehensive proposal ... covering all sections").
# Yet the role-aware gpt-4o-mini verifier judge scored each PASS-issuing verifier
# 0.27 ("your PASS was wrong"), hallucinating that "the artifact is not visible"
# even though it was embedded in the payload. The engine's verdict_scored_incorrect
# route then opened a verification_gap purely on that unreliable score, with NO
# cross-check against the ok terminal — a false incident on a healthy run, whose
# note even asserted "the terminal output is bad" while quoting the OK reasoning.

OK = TerminalVerdict(
    bad=False,
    score=1.0,
    reasoning="comprehensive proposal covering all necessary sections",
    checkable=True,
)


def _flawed(rid, s, flags):
    """A verifier whose own judge believed it could not use its input (the
    'artifact not visible' hallucination) marks input_flawed=True — so the node
    is excluded from cut-point candidacy and the graph reaches the very
    unclassified -> verification_gap upgrade the false incident came through."""
    return NodeScore(
        run_id=rid, score=s, components={}, input_flawed=True,
        unscored_reason=None, judge_note=None, flags=tuple(flags),
    )


def test_passing_verifiers_scored_low_but_terminal_ok_is_no_gap(mk):
    """PASS + terminal ok => NOT a gap. A low role-aware score alone (the judge
    that hallucinated 'artifact not visible') cannot manufacture a gap that
    ground truth — a good, checkable deliverable — flatly contradicts. No origin
    localises, so pre-fix this reached the unclassified -> verification_gap
    upgrade (culprits qa+eval, confidence 0.6, a note asserting the terminal was
    bad while quoting the OK reasoning). Now it stays a non-incident."""
    nodes = ["render", "qa", "eval"]
    edges = [("render", "qa"), ("qa", "eval")]
    scores = {
        "render": _sc("render", 0.9),
        "qa": _flawed("qa", 0.27, ["issued_pass"]),
        "eval": _flawed("eval", 0.27, ["issued_pass"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=OK))

    assert report.report_type == "unclassified"   # not verification_gap
    assert report.evidence.verification_gaps == []
    assert report.culprit_run_ids == []           # no incident-worthy culprit
    # And the report never claims the terminal is bad while it is ok.
    assert not any("terminal output is bad" in n for n in report.evidence.notes)
    assert not any("terminal is bad" in n for n in report.evidence.notes)


def test_bad_terminal_passed_bad_terminal_route_still_fires(mk):
    """Guard: the cross-check must NOT weaken the corroborated case. Healthy
    PASS-issuing verifiers over a BAD, checkable terminal are still gaps via the
    passed_bad_terminal route, and the note still cites the bad terminal."""
    scores = {
        "start": NodeScore(run_id="start", score=None, components={},
                           input_flawed=None, unscored_reason="payload_missing",
                           judge_note=None),
        "think": _sc("think", 0.9), "act": _sc("act", 0.88),
        "render": _sc("render", 0.85), "qa": _sc("qa", 0.92),
        "eval": _sc("eval", 0.93),
    }
    report = find_blame(_incident(mk, scores=scores))  # default terminal BAD

    assert report.report_type == "verification_gap"
    assert set(report.culprit_run_ids) == {"qa", "eval"}
    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {"qa": "passed_bad_terminal", "eval": "passed_bad_terminal"}
    assert any("terminal output is bad" in n for n in report.evidence.notes)
    assert any(BAD.reasoning in n for n in report.evidence.notes)


def test_verdict_scored_incorrect_wrong_pass_still_fires_with_bad_terminal(mk):
    """A PASS scored low by the role-aware judge IS a real gap when a bad,
    checkable terminal corroborates the badness — the cross-check keeps this."""
    nodes = ["gen", "qa"]
    edges = [("gen", "qa")]
    scores = {
        "gen": _sc("gen", 0.9),
        "qa": _flagged("qa", 0.27, ["issued_pass"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))
    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {"qa": "verdict_scored_incorrect"}


def test_wrong_fail_with_ok_terminal_surfaces_as_gap_with_honest_note(mk):
    """A verifier that issued FAIL and scored low is an over-strict false alarm
    ONLY when ground truth — an ok, checkable terminal — confirms the failed work
    was actually fine. That stays a verification_gap, and the upgrade note names
    it a false alarm the ok terminal contradicts, NEVER asserting a bad terminal.
    (A FAIL verifier believed its input was flawed, so it is excluded from
    cut-point candidacy and the graph reaches the upgrade.)"""
    nodes = ["gen", "review"]
    edges = [("gen", "review")]
    scores = {
        "gen": _sc("gen", 0.9),
        "review": _flawed("review", 0.27, ["issued_fail"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=OK))

    assert report.report_type == "verification_gap"
    assert report.culprit_run_ids == ["review"]
    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {"review": "verdict_scored_incorrect"}
    # Honest note: false alarm confirmed by the ok terminal, NOT a bad terminal.
    note = next(n for n in report.evidence.notes if n.startswith("verification_gap"))
    assert "false alarm" in note
    assert "terminal output is bad" not in note
    assert "ok" in note


def test_wrong_fail_with_bad_terminal_is_not_manufactured(mk):
    """A FAIL scored low over a BAD terminal has no ground truth that the FAIL
    was wrong (the work really was bad) — do not manufacture a wrong-FAIL gap."""
    nodes = ["gen", "review"]
    edges = [("gen", "review")]
    scores = {
        "gen": _sc("gen", 0.9),
        "review": _flawed("review", 0.27, ["issued_fail"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))
    assert report.evidence.verification_gaps == []
