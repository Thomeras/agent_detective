"""S23: confidence formula properties — monotonic in drop, penalties multiply,
unknown-ancestor cap, clamped to [0, 1]."""

import pytest

from blame_engine import BlameConfig, Candidate, compute_confidence

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
