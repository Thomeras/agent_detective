"""Algorithm 4: confidence formula (spec 3.6).

raw = 0.5 * gap_term + 0.3 * severity_term + 0.2 * pred_term, then
multiplicative penalties (multi-member SCC, multi-culprit), then the
unknown-ancestor cap, clamped to [0, 1].
"""

from .cutpoint import Candidate
from .types import BlameConfig


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compute_confidence(
    candidate: Candidate, config: BlameConfig, *, multi_culprit: bool = False
) -> float:
    drop = candidate.drop if candidate.drop is not None else 0.0
    gap_term = _clamp(drop / 0.5)
    severity_term = (
        _clamp((config.threshold - candidate.score) / config.threshold)
        if config.threshold > 0
        else 0.0
    )
    pred_term = (
        _clamp((candidate.base - config.threshold) / (1 - config.threshold))
        if candidate.base is not None and config.threshold < 1
        else 0.0
    )
    raw = 0.5 * gap_term + 0.3 * severity_term + 0.2 * pred_term

    confidence = raw
    if candidate.iterations > 1:
        confidence *= config.scc_confidence_penalty
    if multi_culprit:
        confidence *= config.multi_culprit_penalty
    if candidate.unknown_upstream:
        confidence = min(confidence, config.unknown_confidence_cap)
    return _clamp(confidence)


# Deterministic defect signals (a contract violation, an admitted content flag)
# make the "is this output defective?" question near-certain — a hard check, not
# a graded judge opinion. 0.95 rather than 1.0 leaves room for an
# instrumentation artifact; it is never a bare 1.0 "certainty".
_DETERMINISTIC_OBSERVATION = 0.95


def compute_observation_confidence(
    candidate: Candidate, config: BlameConfig, *, deterministic: bool = False
) -> float:
    """How sure we are the culprit's OUTPUT is defective — independent of whether
    the fault ORIGINATED here (that is attribution, above).

    A deterministic signal (contract violation / content flag) pins this near
    certain. Otherwise it scales with severity: how far the score fell below the
    quality threshold. This is deliberately NOT the gap/pred formula — a node can
    be certainly-bad (severity high) while its origin is uncertain (gap unknown).
    """
    if deterministic:
        return _DETERMINISTIC_OBSERVATION
    if config.threshold <= 0:
        return 0.0
    return _clamp((config.threshold - candidate.score) / config.threshold)
