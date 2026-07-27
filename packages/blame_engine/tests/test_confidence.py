"""S23: confidence formula properties — monotonic in drop, penalties multiply,
unknown-ancestor cap, clamped to [0, 1]."""

from dataclasses import replace

import pytest

from blame_engine import BlameConfig, Candidate, compute_confidence
from blame_engine.confidence import (
    DETERMINISTIC_ATTRIBUTION,
    JUDGED_DEGRADATION_OBSERVATION,
    compute_observation_confidence,
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
