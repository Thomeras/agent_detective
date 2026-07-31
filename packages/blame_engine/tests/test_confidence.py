"""S23: confidence formula properties — monotonic in drop, penalties multiply,
unknown-ancestor cap, clamped to [0, 1]."""

from dataclasses import replace

import pytest

from blame_engine import BlameConfig, Candidate, NodeScore, compute_confidence, find_blame
from blame_engine.confidence import (
    DETERMINISTIC_ATTRIBUTION,
    JUDGED_DEGRADATION_OBSERVATION,
    SINGLE_CHANNEL_CUT_POINT_CAP,
    chain_penalty,
    compute_observation_confidence,
    diversity_cap,
)

CFG = BlameConfig()


def _candidate(drop=None, score=0.4, base=1.0, unknown=False, iterations=1):
    return Candidate(
        super_id=0,
        run_id="c",
        score=score,
        base=base,
        drop=drop,
        unknown_upstream=unknown,
        is_source=True,
        iterations=iterations,
        end_time=None,
    )


def test_confidence_monotonic_in_drop() -> None:
    values = [compute_confidence(_candidate(drop=d), CFG) for d in (0.1, 0.2, 0.3, 0.4, 0.5)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)  # strictly increasing here


def test_penalties_multiply() -> None:
    base_conf = compute_confidence(_candidate(drop=0.8, score=0.2), CFG)
    assert base_conf == pytest.approx(0.88)

    scc = compute_confidence(_candidate(drop=0.8, score=0.2, iterations=3), CFG)
    assert scc == pytest.approx(base_conf * CFG.scc_confidence_penalty)

    multi = compute_confidence(_candidate(drop=0.8, score=0.2), CFG, multi_culprit=True)
    assert multi == pytest.approx(base_conf * CFG.multi_culprit_penalty)

    both = compute_confidence(
        _candidate(drop=0.8, score=0.2, iterations=3), CFG, multi_culprit=True
    )
    assert both == pytest.approx(
        base_conf * CFG.scc_confidence_penalty * CFG.multi_culprit_penalty
    )


def test_unknown_ancestor_caps_confidence() -> None:
    high = compute_confidence(_candidate(drop=0.8, score=0.2, unknown=True), CFG)
    assert high == pytest.approx(CFG.unknown_confidence_cap)  # 0.88 -> capped at 0.6

    # A value already below the cap passes through unchanged.
    low = compute_confidence(_candidate(drop=0.2, score=0.4, unknown=True), CFG)
    assert low == pytest.approx(0.5 * 0.4 + 0.3 * 0.2 + 0.2 * 1.0)


def test_confidence_clamped_to_unit_interval() -> None:
    maximal = compute_confidence(_candidate(drop=5.0, score=0.0, base=1.0), CFG)
    assert maximal == pytest.approx(1.0)
    assert 0.0 <= maximal <= 1.0

    minimal = compute_confidence(_candidate(drop=0.0, score=0.5, base=0.4), CFG)
    assert minimal == pytest.approx(0.0)
    assert 0.0 <= minimal <= 1.0


def test_none_drop_and_none_base_contribute_zero() -> None:
    conf = compute_confidence(_candidate(drop=None, score=0.3, base=None), CFG)
    assert conf == pytest.approx(0.3 * 0.4)  # severity term only


# --- observation confidence: "is this output defective?" ------------------
#
# Two independent readings, and the stronger wins. Severity alone answers "did it
# ship something unusable" and is SILENT about a node that halved the quality and
# still cleared the bar — which is how a node named as the place quality broke
# came to be rendered with a 0 % observation meter beside that claim.


def test_observation_reads_a_measured_drop_when_severity_says_nothing() -> None:
    """The reported case: score exactly AT the threshold, so severity is 0.0,
    while the node demonstrably took 1.00 and produced 0.50."""
    at_threshold = _candidate(drop=0.5, score=0.5, base=1.0)
    assert compute_observation_confidence(at_threshold, CFG) == pytest.approx(
        JUDGED_DEGRADATION_OBSERVATION
    )
    # And above the threshold, where severity is not merely 0 but negative.
    above = _candidate(drop=0.35, score=0.6, base=0.95)
    assert compute_observation_confidence(above, CFG) == pytest.approx(0.7 * 0.7)


def test_observation_takes_the_stronger_reading_never_a_blend() -> None:
    """A deeply degraded node keeps its severity-based number: the weaker
    degradation reading may not dilute it."""
    deep = _candidate(drop=0.9, score=0.05, base=0.95)
    severity = (CFG.threshold - 0.05) / CFG.threshold  # 0.9
    degradation = 1.0 * JUDGED_DEGRADATION_OBSERVATION  # 0.7
    assert severity > degradation
    assert compute_observation_confidence(deep, CFG) == pytest.approx(severity)


def test_observation_never_reads_an_assumed_baseline() -> None:
    """An assumed 1.0 source baseline is a fiction; manufacturing observation
    confidence out of it is the fabricated number ``base_assumed`` exists to
    prevent. A boundary origin reports severity alone."""
    assumed = replace(_candidate(drop=0.9, score=0.1, base=1.0), base_assumed=True)
    measured = _candidate(drop=0.9, score=0.1, base=1.0)
    severity = (CFG.threshold - 0.1) / CFG.threshold  # 0.8
    assert compute_observation_confidence(assumed, CFG) == pytest.approx(severity)
    # With the same numbers MEASURED, the drop reading is available (and here
    # still loses to severity) — the only difference is the fiction.
    assert compute_observation_confidence(measured, CFG) == pytest.approx(severity)
    weak_severity = replace(_candidate(drop=0.9, score=0.5, base=1.0), base_assumed=True)
    assert compute_observation_confidence(weak_severity, CFG) == pytest.approx(0.0)


def test_deterministic_observation_is_unaffected_by_the_drop_reading() -> None:
    cand = _candidate(drop=0.5, score=0.5, base=1.0)
    assert compute_observation_confidence(
        cand, CFG, deterministic=True
    ) == pytest.approx(DETERMINISTIC_ATTRIBUTION)


def test_observation_ceiling_is_the_judged_finding_certainty() -> None:
    """Not an invented constant: a judged content Finding carries certainty 0.7,
    so an observation resting entirely on judged scores cannot be worth more."""
    assert JUDGED_DEGRADATION_OBSERVATION == 0.7
    huge = _candidate(drop=5.0, score=0.5, base=1.0)
    assert compute_observation_confidence(huge, CFG) == pytest.approx(0.7)


def test_fail_signal_that_observed_origination_carries_the_headline(mk):
    """The bug the foreign corpus found: a node localised by a fail-severity
    deterministic signal was scored as if no deterministic evidence existed,
    because the confidence predicate read a CLOSED SET of three flag names while
    the localisation predicate read any fail signal. The report then cited a
    100%-certainty finding beside `confidence 0%`."""
    empty = {
        "name": "empty_output", "severity": "fail", "code": "empty_output_with_spend",
        "params": {"tokens_out": 1200, "chars": 0},
        "detail": "produced no output while spending 1200 output tokens",
        "basis": "output.value recorded and empty", "originates": True,
    }
    inp = mk(
        nodes=["src", "sink"],
        edges=[("src", "sink")],
        scores={
            "src": 0.93,
            "sink": NodeScore(
                run_id="sink", score=None, components={}, input_flawed=None,
                unscored_reason="empty_output", judge_note=None,
                flags=("empty_output",), deterministic_signals=(empty,),
            ),
        },
    )
    report = find_blame(inp)

    assert "sink" in report.culprit_run_ids
    assert report.evidence.observation_confidence == pytest.approx(0.95)
    assert report.evidence.attribution_confidence == pytest.approx(0.95)


def test_judged_content_flag_is_not_deterministic_evidence(mk):
    """The other half of the same drift. `missing_required_content` is a string an
    LLM judge emits — it is recorded as a judged Finding worth certainty 0.7 — yet
    the observation predicate counted it as deterministic and published 0.95, the
    constant that means "origination observed on BOTH sides of the fault", for a
    node whose contract_violations and deterministic_signals are both empty. One
    flag cannot be worth 0.7 as evidence and 0.95 as proof."""
    inp = mk(
        nodes=["src", "sink"],
        edges=[("src", "sink")],
        scores={
            "src": 0.90,
            "sink": NodeScore(
                run_id="sink", score=0.55, components={}, input_flawed=None,
                unscored_reason=None, judge_note="lacks the rental detail",
                flags=("missing_required_content",),
            ),
        },
    )
    report = find_blame(inp)

    assert "sink" in report.culprit_run_ids
    obs = report.evidence.observation_confidence
    assert obs != pytest.approx(DETERMINISTIC_ATTRIBUTION)
    # It falls to what the judged channel actually measured: a drop of 0.35
    # against the 0.5 saturation point, worth JUDGED_DEGRADATION_OBSERVATION.
    assert obs == pytest.approx((0.35 / 0.5) * JUDGED_DEGRADATION_OBSERVATION)


def test_observation_and_localisation_share_one_deterministic_predicate(mk):
    """Two functions answering the same question drifted apart twice. They are one
    function now, so a third drift cannot be written."""
    from blame_engine import blame as _blame
    from blame_engine import cutpoint as _cutpoint

    assert _blame._has_deterministic_defect is _cutpoint._deterministic_defect


def test_fail_signal_without_the_marker_keeps_inferred_attribution(mk):
    """An injection signature says the output is bad and nothing about where it
    came from — it may well have arrived from upstream. No marker, no headline."""
    unmarked = {
        "name": "injection_signature", "severity": "fail", "code": "injection",
        "params": {}, "detail": "d", "basis": "b",
    }
    inp = mk(
        nodes=["src", "sink"],
        edges=[("src", "sink")],
        scores={
            "src": 0.93,
            "sink": NodeScore(
                run_id="sink", score=None, components={}, input_flawed=None,
                unscored_reason=None, judge_note=None,
                flags=("injection_signature",), deterministic_signals=(unmarked,),
            ),
        },
    )
    report = find_blame(inp)
    assert report.evidence.attribution_confidence < 0.95


# --- Chain shape and channel diversity (P5 / P1b) ---------------------------


def test_chain_penalty_scales_with_the_length_it_cannot_discriminate():
    # A 3-step line still narrows the origin to one interior node; an 18-step
    # one narrows it to seventeen, which is not narrowing. A flat discount
    # would have charged both the same.
    short = chain_penalty(3, CFG)
    long = chain_penalty(18, CFG)
    assert short > long
    assert long == pytest.approx(CFG.chain_confidence_penalty)


def test_chain_penalty_saturates_and_never_exceeds_the_configured_floor():
    assert chain_penalty(30, CFG) == pytest.approx(CFG.chain_confidence_penalty)
    assert chain_penalty(200, CFG) == pytest.approx(CFG.chain_confidence_penalty)


def test_chain_penalty_absent_when_there_is_no_interior_node_to_confuse():
    # Head and tail are never in question, so a 2-node line withholds nothing.
    assert chain_penalty(2, CFG) == 1.0
    assert chain_penalty(0, CFG) == 1.0


def test_single_channel_cut_point_is_capped_below_corroborated_localisation():
    # Naming ONE origin on one instrument's word cannot reach the certainty of
    # a cut_point two independent channels agree on.
    assert diversity_cap("cut_point", single_channel=True) == SINGLE_CHANNEL_CUT_POINT_CAP
    assert diversity_cap("cut_point", single_channel=False) == 1.0


def test_diversity_cap_only_binds_verdicts_that_name_an_origin():
    # A fallback verdict names no single node, so channel count is not what
    # limits it — its own report-type cap already does.
    assert diversity_cap("composition_failure", single_channel=True) == 1.0


def test_incomplete_channels_discount_attribution_but_absence_of_data_does_not():
    reported = _candidate(drop=0.5)
    thin = replace(reported, score_channels=("judge",), score_channels_all=("schema", "judge"))
    assert compute_confidence(thin, CFG) < compute_confidence(reported, CFG)
    # Legacy candidate: nothing recorded about channels -> no penalty invented.
    assert compute_confidence(reported, CFG) == compute_confidence(
        replace(reported, score_channels=None, score_channels_all=None), CFG
    )
