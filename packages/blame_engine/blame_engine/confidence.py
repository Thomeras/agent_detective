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
