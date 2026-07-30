"""Algorithm 4: confidence formula (spec 3.6).

raw = 0.5 * gap_term + 0.3 * severity_term + 0.2 * pred_term, then
multiplicative penalties (multi-member SCC, missing scoring channel,
chain-shaped graph, tie between equally evidenced origins, multi-culprit),
then the unknown-ancestor cap, clamped to [0, 1].

Every penalty says the same thing in a different dimension: a claim may not
outrun its evidence. What is missing — a channel that never reported, a shape
with no branching to discriminate on, a tie the tie-break resolved by clock —
lowers the number; it never raises it and never changes which node is named.
"""

import math

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

# A cut_point localised on FEWER channels than the scorer weighed stays a
# cut_point. Every entry above is a verdict that names no individual origin
# (composition / external / verification / terminal-unlocalized) or names
# several (multi_culprit); a thin-channel cut_point still names one, backed by a
# measured drop, so renaming it to any of them would assert something the
# evidence does not — that no node broke, or that the fault came from outside,
# or that a verifier failed. This table's design already says where thin
# evidence goes: into the NUMBER, not into the type. So the honesty is carried
# by ``single_channel_penalty`` in the formula below, and by the
# ``single_channel`` note that states which channel never reported.


def report_type_cap(report_type: str) -> float:
    """Honest ceiling for a report type's headline confidence (1.0 = uncapped)."""
    return REPORT_TYPE_CAP.get(report_type, 1.0)


# A cut_point is a POSITIVE claim: the fault originated at this named node. When
# the origin's score came from a single channel, that claim rests on one
# instrument with nothing to corroborate it — the same evidence class as
# JUDGED_DEGRADATION_OBSERVATION, and held to the same ceiling. It stays above
# the "could not localise" verdicts (0.6 and below) because a measured drop does
# point somewhere; it can no longer reach the certainty of a cut_point that two
# independent channels agree on. The multiplier alone was not enough: it scales
# with the evidence, so a strong-looking single channel still arrived near 1.0.
SINGLE_CHANNEL_CUT_POINT_CAP = 0.7


def diversity_cap(report_type: str, *, single_channel: bool) -> float:
    """Ceiling a positive localisation may reach on one channel's evidence."""
    if single_channel and report_type == "cut_point":
        return SINGLE_CHANNEL_CUT_POINT_CAP
    return 1.0


# Backwards-compatible alias: `_DETERMINISTIC_OBSERVATION` was the old private
# name imported by blame.py. It IS the deterministic attribution constant.
_DETERMINISTIC_OBSERVATION = DETERMINISTIC_ATTRIBUTION


def channels_incomplete(
    reported: tuple[str, ...] | None, offered: tuple[str, ...] | None
) -> bool:
    """True when a score rests on FEWER channels than the scorer weighed.

    The renormalization is what makes this backwards by default: a channel that
    never reported hands its weight to the survivors (schema absent ⇒ the
    judge's 0.40 becomes 0.727 of the blend), so a missing measurement made the
    remaining one speak LOUDER. Unknown coverage — a legacy report, an unscored
    node — is not a missing channel and earns no penalty.
    """
    if reported is None or offered is None:
        return False
    return len(reported) < len(offered)


def chain_penalty(depth: int, config: BlameConfig) -> float:
    """How much a chain's shape discounts attribution, scaled by its length.

    A flat penalty would treat a 3-step pipeline like an 18-step one, but they
    are not equally silent: three steps still narrow the origin to one interior
    node, eighteen narrow it to seventeen. The discount therefore ramps with the
    interior length and saturates at ``chain_full_penalty_depth``. The ramp is
    sqrt because the discriminating power falls off fastest at the start — the
    step from 1 candidate to 4 costs far more certainty than 13 to 16 does.
    """
    interior = depth - 2  # the head and the tail are not in question
    full = max(1, config.chain_full_penalty_depth - 2)
    if interior <= 0:
        return 1.0
    ramp = min(1.0, math.sqrt(interior / full))
    return 1.0 - (1.0 - config.chain_confidence_penalty) * ramp


def compute_confidence(
    candidate: Candidate,
    config: BlameConfig,
    *,
    multi_culprit: bool = False,
    chain_depth: int = 0,
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
    if channels_incomplete(candidate.score_channels, candidate.score_channels_all):
        confidence *= config.single_channel_penalty
    # The graph's shape is silent about WHERE, so attribution may not borrow
    # authority from it. Observation is untouched — "is this output defective"
    # is answered by the node's own score, whatever the graph looks like.
    if chain_depth:
        confidence *= chain_penalty(chain_depth, config)
    # k origins with identical evidence are k equally supported answers, and the
    # tie-break names one of them by the clock. Splitting the attribution is what
    # keeps the named node from carrying certainty the tie denies it; the set
    # itself is reported as competing hypotheses.
    if len(candidate.tied_members) > 1:
        confidence /= len(candidate.tied_members)
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
