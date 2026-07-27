"""Algorithm 4: confidence formula (spec 3.6).

raw = 0.5 * gap_term + 0.3 * severity_term + 0.2 * pred_term, then
multiplicative penalties (multi-member SCC, multi-culprit), then the
unknown-ancestor cap, clamped to [0, 1].
"""

from .cutpoint import Candidate
from .types import BlameConfig


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# --- Confidence rules table (verdict refactor §2.2 / §6) -----------------
# ONE place for every hard-coded confidence number that used to be scattered
# across blame.py (the `_CONFIDENCE_CAP` dict, the inline 0.65 fabrication
# constant, the 0.6 observability cap) and confidence.py (the 0.95 deterministic
# override). The table is seeded EXACTLY from today's values — any deliberate
# recalibration is a separate, later change. Nothing else in the engine may
# define a confidence constant; they all read from here.

# A deterministic signal (contract violation / admitted content flag) observed
# BOTH sides of the fault (input intact, output rewritten): origination is
# observed, not inferred → near-certain. 0.95 not 1.0 leaves room for an
# instrumentation artifact; it is never a bare "certainty".
DETERMINISTIC_ATTRIBUTION = 0.95

# Attribution ceiling for a CONTENT defect at the OBSERVABILITY BOUNDARY (its
# baseline is assumed, not measured). "The fault originated here" cannot be
# near-certain about a node whose predecessor was never scored.
BOUNDARY_ATTRIBUTION_CAP = 0.6

# Fabrication cascade: indirect but corroborated evidence (a self-critical
# content flag + bad terminal ground truth). Stronger than the composition guess
# (0.4), weaker than a hard score gap.
CORROBORATED_FLAG = 0.65

# Ceiling for an observation that rests on a MEASURED QUALITY DROP rather than on
# an output that fell below the acceptance bar. Anchored, not invented: 0.7 is the
# certainty this engine already assigns to every judged content Finding
# (``content_score`` / ``content_drop`` / ``content_flag`` in ``build_findings``),
# so an observation carried entirely by judged scores cannot be worth more than
# the findings it reads. It stays below the deterministic 0.95 because a node that
# halved the quality and still cleared the threshold has demonstrably degraded the
# work without having shipped something unusable.
JUDGED_DEGRADATION_OBSERVATION = 0.7

# Per-report-type headline confidence ceilings. Fallback verdicts (we could not
# localise the fault) must never be sold as a sure thing; only cut_point (a real
# score gap) and loop_detected (a deterministic limit breach) keep full
# confidence (absent from the table ⇒ cap 1.0).
REPORT_TYPE_CAP: dict[str, float] = {
    "composition_failure": 0.4,
    "root_cause_external": 0.5,
    "multi_culprit": 0.8,
    "verification_gap": 0.6,
    # Content defect OBSERVED at the terminal (judge ground truth) but with no
    # origin in the score map — same evidence class as verification_gap.
    "terminal_defect_unlocalized": 0.6,
}


def report_type_cap(report_type: str) -> float:
    """Honest ceiling for a report type's headline confidence (1.0 = uncapped)."""
    return REPORT_TYPE_CAP.get(report_type, 1.0)


# Backwards-compatible alias: `_DETERMINISTIC_OBSERVATION` was the old private
# name imported by blame.py. It IS the deterministic attribution constant.
_DETERMINISTIC_OBSERVATION = DETERMINISTIC_ATTRIBUTION


def compute_confidence(
    candidate: Candidate, config: BlameConfig, *, multi_culprit: bool = False
) -> float:
    drop = candidate.drop if candidate.drop is not None else 0.0
    gap_term = _clamp(drop / 0.5)
    # No judged score ⇒ no severity EVIDENCE, so the term contributes nothing.
    # This is not "the score was 0": an unjudged deterministic origin used to
    # arrive here carrying a stand-in 0.0, which maxed the severity term and
    # invented ~0.3 of attribution out of a measurement that never happened.
    severity_term = (
        _clamp((config.threshold - candidate.score) / config.threshold)
        if config.threshold > 0 and candidate.score is not None
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


def compute_observation_confidence(
    candidate: Candidate, config: BlameConfig, *, deterministic: bool = False
) -> float:
    """How sure we are the culprit's OUTPUT is defective — independent of whether
    the fault ORIGINATED here (that is attribution, above).

    A deterministic signal (contract violation / content flag) pins this near
    certain. Otherwise it is the STRONGER of two judged readings, because "is this
    output defective" has two independent answers:

    - **severity** — how far the score fell below the acceptance threshold. This
      answers "did it ship something unusable".
    - **degradation** — a MEASURED drop from a scored predecessor. This answers
      "did this node make the work worse", which severity is completely silent
      about: a node that took 1.00 and produced exactly the threshold 0.50 scored
      severity 0.0, so the report named it the place quality broke while its own
      observation meter read 0 %. The drop of 0.50 was the evidence; the number
      just was not reading it.

    Only a drop against a SCORED predecessor counts. An assumed 1.0 source
    baseline is a fiction, and manufacturing observation confidence out of it is
    the fabricated number ``base_assumed`` exists to prevent — so a boundary
    origin still reports severity alone.

    This is deliberately NOT the gap/pred formula: a node can be certainly-bad
    (severity high) while its origin is uncertain (gap unknown).
    """
    if deterministic:
        return _DETERMINISTIC_OBSERVATION
    # Same rule as the attribution formula: an unjudged node has no severity
    # evidence, and a stand-in score would fabricate one.
    severity = (
        _clamp((config.threshold - candidate.score) / config.threshold)
        if config.threshold > 0 and candidate.score is not None
        else 0.0
    )
    degradation = 0.0
    if candidate.drop is not None and not candidate.base_assumed:
        # Same normalisation as the gap term in compute_confidence (a drop of 0.5
        # saturates), scaled by what a judged content finding is worth.
        degradation = _clamp(candidate.drop / 0.5) * JUDGED_DEGRADATION_OBSERVATION
    # max(), never a blend: each is independent evidence that the output is
    # defective, so the weaker one may not dilute the stronger.
    return _clamp(max(severity, degradation))
