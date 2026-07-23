"""Edge-drop origin model (revised Algorithm 3) + verification_gap.

Regression fixtures for the "blame the node that broke, not the orchestrator"
and "verifier rubber-stamping" failure modes surfaced by a real run.
"""

from blame_engine import NodeScore, TerminalVerdict, find_blame

BAD = TerminalVerdict(bad=True, score=0.1, reasoning="final output has no content")


def _sc(rid, s, flawed=None):
    return NodeScore(
        run_id=rid, score=s, components={}, input_flawed=flawed,
        unscored_reason=None, judge_note=None,
    )


def test_spurious_low_source_does_not_shadow_real_downstream_origin(mk):
    """The wedge fixture: pipeline (source) is spuriously low because it is
    scored on the whole task, but quality recovered right after it (think 0.93).
    Quality actually broke at render (0.93 -> 0.56). Blame must land on render —
    the origin — not on the low source."""
    nodes = ["pipeline", "think", "act", "render", "qa", "eval"]
    edges = [("pipeline", "think"), ("think", "act"), ("act", "render"),
             ("render", "qa"), ("qa", "eval")]
    scores = {
        "pipeline": _sc("pipeline", 0.35), "think": _sc("think", 0.93),
        "act": _sc("act", 0.93), "render": _sc("render", 0.56),
        "qa": _sc("qa", 0.93), "eval": _sc("eval", 0.93),
    }
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores))

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["render"]
    # The failure surfaced in the terminal ARTIFACT — render's output. The eval
    # sink is a verifier: it issued a verdict about that artifact, it did not
    # manifest anything, so the verifier sink maps back to the producer.
    assert report.evidence.manifestation_run_ids == ["render"]


def test_faulty_source_that_propagates_is_still_blamed(mk):
    """A source that is bad AND whose degradation propagates (successor also
    low) remains the culprit — it is the origin of the degraded region."""
    inp = mk(nodes=["s", "t"], edges=[("s", "t")], scores={"s": _sc("s", 0.2), "t": _sc("t", 0.25)})
    report = find_blame(inp)
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["s"]


def test_recovered_low_source_is_blamed_only_when_alone(mk):
    """A low source whose successor recovered is still the culprit when nothing
    downstream broke (no better origin exists)."""
    inp = mk(nodes=["s", "t"], edges=[("s", "t")], scores={"s": _sc("s", 0.3), "t": _sc("t", 0.9)})
    assert find_blame(inp).culprit_run_ids == ["s"]


def test_loop_blame_drills_into_worst_member(mk):
    """When the culprit is a retry loop (SCC), blame the worst-scoring member
    (where quality actually broke) — not the loop's exit node."""
    nodes = ["think", "act", "render", "qa", "eval"]
    edges = [("think", "act"), ("act", "render"), ("render", "qa"),
             ("qa", "eval"), ("eval", "act")]  # eval -> act closes the loop
    scores = {"think": _sc("think", 0.9), "act": _sc("act", 0.93),
              "render": _sc("render", 0.27), "qa": _sc("qa", 0.93), "eval": _sc("eval", 0.56)}
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores))
    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["render"]  # not the loop exit "eval"
    assert report.evidence.drops["render"] > 0.6  # real break vs act, not vs think


def test_verification_gap_flags_low_scoring_verifiers(mk):
    """Role-aware judging scores a rubber-stamping verifier LOW (wrong verdict).
    Those surface as verification gaps; an honest verifier does not. Here gen
    produced bad work, qa and review passed it -> both are gaps."""
    nodes = ["gen", "qa", "review"]
    edges = [("gen", "qa"), ("qa", "review")]
    scores = {"gen": _sc("gen", 0.3), "qa": _sc("qa", 0.2), "review": _sc("review", 0.2)}
    report = find_blame(
        mk(nodes=nodes, edges=edges, scores=scores,
           agent_names={"gen": "generator", "qa": "qa", "review": "review"},
           terminal_verdict=BAD)
    )
    gaps = {g["run_id"] for g in report.evidence.verification_gaps}
    assert gaps == {"qa", "review"}


def test_structural_root_does_not_block_composition_failure(mk):
    """An unscored orchestrator root (source, no output -> payload_missing) must
    not turn an all-healthy-but-terminal-bad graph into 'unclassified' with $0
    cost. It is a structural entry point, not a hidden culprit."""
    root = NodeScore(run_id="start", score=None, components={}, input_flawed=None,
                     unscored_reason="payload_missing", judge_note=None)
    nodes = ["start", "think", "act"]
    edges = [("start", "think"), ("think", "act")]
    scores = {"start": root, "think": _sc("think", 0.9), "act": _sc("act", 0.9)}
    report = find_blame(mk(nodes=nodes, edges=edges, scores=scores, terminal_verdict=BAD))
    assert report.report_type == "composition_failure"
    assert report.downstream_cost_usd > 0  # cost is attributed, not $0
