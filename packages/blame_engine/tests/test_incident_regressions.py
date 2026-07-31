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
from conftest import candidacy_of, note_of, verdict_of

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
    assert note_of(report, "cut_point")["variant"] == "cumulative"


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
    assert verdict_of(report, "qa") == "gap_passed_bad_terminal"
    assert verdict_of(report, "eval") == "gap_passed_bad_terminal"


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
    assert note_of(report, "terminal_not_checkable") is not None
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
    conflict = note_of(report, "verdict_conflict")
    assert conflict is not None
    # The note carries the terminal's OWN reasoning, so a report leaning on
    # "terminal is bad" cannot be rendered without showing that evidence.
    assert conflict["terminal_reasoning"] == BAD.reasoning


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
    assert list(report.evidence.candidacy_records) == report.evidence.topo_order
    assert report.evidence.verifier_run_ids == ["qa", "eval"]


def test_candidacy_carries_the_numbers_behind_each_decision(mk):
    """The trace must be auditable: score vs threshold, drop vs reference,
    exclusion reason — not bare labels."""
    report = find_blame(_incident(mk))

    assert verdict_of(report, "start") == "structural_root"
    assert verdict_of(report, "think") == "degradation_path_start"
    assert candidacy_of(report, "think")["score"] == pytest.approx(0.89)
    assert verdict_of(report, "act") == "origin_cumulative"
    assert candidacy_of(report, "act")["drop"] == pytest.approx(0.33)
    assert verdict_of(report, "render") == "degradation_path_member"
    assert candidacy_of(report, "render")["score"] == pytest.approx(0.56)
    assert verdict_of(report, "qa") == "gap_passed_bad_terminal"
    assert candidacy_of(report, "qa")["score"] == pytest.approx(0.92)


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
    assert verdict_of(report, "o") == "composition_suspect"


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
    assert note_of(report, "cut_point")["variant"] == "fabrication"
    assert verdict_of(report, "think") == "origin_fabrication"
    # Verifiers that passed the bad work stay flagged in evidence.
    assert {g["run_id"] for g in report.evidence.verification_gaps} == {"qa", "eval"}
    # The producer's healthy score is confronted, not left standing as fact.
    assert note_of(report, "claims_vs_reality", agent="render") is not None
    assert verdict_of(report, "render") == "claims_conflict"


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
    reasons = [r["code"] for r in note_of(report, "unclassified")["reasons"]]
    assert reasons == ["no_terminal_verdict"]


def test_candidacy_never_misstates_a_comparison(mk):
    """Regression for the 'origin — score 0.93 < threshold 0.50' lie: every
    numeric claim in the candidacy trace must be true for its node."""
    report = find_blame(_incident(mk))
    # Verdicts whose template asserts "score < threshold" may only be assigned
    # to nodes for which that is TRUE. The claim is now a property of the
    # verdict code, so it is checkable without reading a sentence.
    below_threshold_verdicts = {
        "origin_boundary", "inherited", "independent_low", "below_not_origin",
        "gap_verdict_scored_incorrect",
    }
    for run_id, rec in report.evidence.candidacy_records.items():
        if rec["verdict"] in below_threshold_verdicts:
            score = report.evidence.score_map[run_id]
            assert score is not None and score < 0.5, (run_id, rec)


def test_structural_root_labelled_by_design_no_warning(mk):
    """start has no payload BY DESIGN — candidacy says so, and there is no
    instrumentation warning for it. A non-root payload gap does warn."""
    report = find_blame(_incident(mk))
    assert verdict_of(report, "start") == "structural_root"
    assert note_of(report, "instrumentation_warning") is None

    broken = NodeScore(run_id="mid", score=None, components={}, input_flawed=None,
                       unscored_reason="payload_missing", judge_note=None)
    inp = mk(nodes=["a", "mid", "b"], edges=[("a", "mid"), ("mid", "b")],
             scores={"a": 0.9, "mid": broken, "b": 0.9})
    report2 = find_blame(inp)
    assert note_of(report2, "instrumentation_warning")["agents"] == ["mid"]


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
    assert verdict_of(report, "eval") == "whistleblower"


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

    assert verdict_of(report, "act") == "cascade_participant"
    # render is the manifestation producer, so the stronger claims-vs-reality
    # verdict wins there — either way its score reads as an unverified claim.
    assert verdict_of(report, "render") == "claims_conflict"
    assert note_of(report, "cascade_participants")["agents"] == ["act", "render"]

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
    assert note_of(report, "verification_gap") is None
    assert note_of(report, "verdict_conflict") is None


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
    gap_note = note_of(report, "verification_gap")
    assert {g["basis"] for g in gap_note["gaps"]} == {"passed_bad_terminal"}
    # The bad terminal is cited as ground truth, with its own reasoning.
    assert gap_note["terminal"] == "bad"
    assert gap_note["terminal_reasoning"] == BAD.reasoning


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
    # Honest note: a wrong-FAIL confirmed by the OK terminal. The false-alarm
    # wording is reachable only from (issued_fail=True, terminal="ok") — with a
    # bad terminal the template could not render it.
    gap_note = note_of(report, "verification_gap")
    assert gap_note["terminal"] == "ok"
    assert gap_note["gaps"] == [
        {"agent": "review", "score": pytest.approx(0.27),
         "basis": "verdict_scored_incorrect", "issued_fail": True}
    ]


def test_wrong_fail_with_bad_terminal_is_reported_as_a_conflict_not_a_verdict(mk):
    """A FAIL scored low over a BAD terminal is self-contradictory: a FAIL on bad
    work is the RIGHT call, so the flag and the sub-threshold score cannot both
    hold. The engine has no ground truth that the FAIL was wrong, so it still may
    not manufacture a wrong-FAIL gap — but dropping the node reported nothing at
    all, which is how a verifier that let bad work through disappeared from the
    report on the strength of one unchecked judge string. It is surfaced as an
    UNRESOLVED conflict instead."""
    nodes = ["gen", "review"]
    edges = [("gen", "review")]
    scores = {
        "gen": _sc("gen", 0.9),
        "review": _flawed("review", 0.27, ["issued_fail"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))

    gaps = {g["run_id"]: g["basis"] for g in report.evidence.verification_gaps}
    assert gaps == {"review": "verifier_flag_conflict"}
    # The original guarantee, unchanged: never assert the FAIL was wrong.
    assert "verdict_scored_incorrect" not in gaps.values()


def test_flag_conflict_never_promotes_the_verifier_to_culprit(mk):
    """The conflict says the engine cannot tell which of the two judge outputs
    failed. Blaming the verifier on it would replace a silent drop with a louder
    unfounded claim, so the report type and culprits stay as the evidence left
    them."""
    nodes = ["gen", "review"]
    edges = [("gen", "review")]
    scores = {
        "gen": _sc("gen", 0.9),
        "review": _flawed("review", 0.27, ["issued_fail"]),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores,
                           terminal_verdict=BAD))

    assert report.report_type != "verification_gap"
    assert "review" not in report.culprit_run_ids
    # Reported, not swallowed: the gap and its finding both exist.
    assert report.evidence.verification_gaps
    assert any(
        f["kind"] == "verifier_verdict"
        and f["data"].get("basis") == "verifier_flag_conflict"
        for f in report.evidence.findings
    )
