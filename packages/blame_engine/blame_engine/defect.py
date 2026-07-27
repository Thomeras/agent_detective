"""Defect — an interpreted fault with its OWN origin resolution (§2.2).

A Defect is what localization produces from Finding[]: one fault, on one channel,
with one origin resolution, carrying references back to the findings that show it.

The point of the type is that **Origin is a sum type**. A defect without a
localizable candidate is ``Unlocalized`` *by construction* — the run-C bug (a
cut_point claiming a content defect its own evidence does not show) becomes an
unrepresentable state rather than an elif branch that has to remember the case.

Structured caveats are FIELDS, not prose (§2.4 render rules): ``base_assumed``,
``observability_boundary`` and ``unverified_in_channel`` render as chips that can
never be truncated away mid-sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# --- Finding references with POLARITY ------------------------------------
# A ref says which finding a defect cites AND what the finding shows for it:
#   supporting — the finding asserts the defect (evidence FOR)
#   refuting   — the finding contradicts the defect (counter-evidence; kept so
#                a tension is visible instead of silently dropped)
#   context    — related measurement that neither proves nor disproves
# Polarity is about what the finding CLAIMS relative to the defect; reliability
# of the claim stays in Finding.certainty. A defect that cites only exculpatory
# evidence ("the evidence for this defect": render 1.0 + terminal ok) was the
# run-15 bug — the validator below makes it unrepresentable.

RefRole = Literal["supporting", "refuting", "context"]


@dataclass(frozen=True)
class FindingRef:
    ref: int                      # index into the report's findings[]
    role: RefRole = "supporting"

# --- Origin sum type -----------------------------------------------------


@dataclass(frozen=True)
class Localized:
    """The fault originated at a specific run and we can say which."""

    run_id: str


# Reason CODES for the non-localized origins. A reason is an identifier, not a
# sentence: the phrasing lives in one narrative template (§2.4), so a stored
# defect can be re-rendered, queried and compared across runs without anyone
# parsing English. Legacy schema-2 payloads hold prose here; the renderer prints
# an unknown code verbatim, so old reports keep reading exactly as they did.
REASON_ORCHESTRATION_LAYER = "orchestration_layer"
REASON_NO_CONTENT_CANDIDATE = "no_content_candidate"
REASON_INPUT_ALREADY_FLAWED = "input_already_flawed"
REASON_NO_FORM_VERIFIER = "no_form_verifier"


@dataclass(frozen=True)
class Unlocalized:
    """The fault is OBSERVED but no node qualifies as its origin. Carries the
    reason CODE so the report can explain why localization failed instead of
    pinning the fault on an innocent node."""

    reason: str


@dataclass(frozen=True)
class External:
    """The fault entered from outside the observed graph (an upstream/source
    reported its own input already flawed)."""

    run_id: str | None = None
    reason: str = REASON_INPUT_ALREADY_FLAWED


@dataclass(frozen=True)
class Design:
    """No node owns this fault — it is a gap in the graph's DESIGN (e.g. no
    verifier charter covers form/contract vision)."""

    reason: str


Origin = Localized | Unlocalized | External | Design

DefectKind = Literal["contract", "content", "form", "loop", "verification"]
Channel = Literal["deterministic", "judged"]


@dataclass(frozen=True)
class Defect:
    kind: str                              # contract | content | form | loop | verification
    channel: Channel
    origin: Origin
    finding_refs: tuple[FindingRef, ...] = ()  # typed refs into findings[]
    observation_confidence: float | None = None   # is the output defective?
    attribution_confidence: float | None = None    # did it originate at `origin`?
    propagation: tuple[str, ...] = ()      # downstream run_ids it flowed through
    # Typed caveats (§2.4). Rendered as chips, exempt from prose clipping.
    base_assumed: bool = False             # baseline assumed, not measured
    observability_boundary: bool = False   # origin sits at the observable edge
    unverified_in_channel: str | None = None  # e.g. "contract" still unverified
    # This node underperformed but every successor recovered and the terminal
    # deliverable is ok — a near-miss the pipeline compensated for, NOT a live
    # break. A recovered content defect keeps its origin (the fragile node) but
    # must never derive a cut_point (§3 content-only degraded_recovered row).
    recovered: bool = False
    # RUN-LEVEL evidence fact: the judged quality channel produced no score for
    # any node on this run (a `--no-judge` pass, or every node unscored because
    # the composite never cleared the minimum weight). A deterministic defect
    # then stands entirely on its own hard check and NOTHING may be inferred
    # about content quality — least of all "recovered", which needs scored
    # healthy successors and a checkable terminal. It cannot conflict with
    # ``recovered``: recovery is unobservable without scores. Legacy payloads
    # lack the key and default to False, so stored evidence derives unchanged.
    quality_unmeasured: bool = False


def supporting_refs(d: Defect) -> tuple[int, ...]:
    """Indices of the findings that ASSERT this defect."""
    return tuple(r.ref for r in d.finding_refs if r.role == "supporting")


def origin_run_id(origin: Origin) -> str | None:
    """The run a defect is pinned to, or None when it is not Localized."""
    if isinstance(origin, Localized):
        return origin.run_id
    if isinstance(origin, External):
        return origin.run_id
    return None


def _serialize_origin(o: Origin) -> dict:
    tag = type(o).__name__
    return {"kind": tag, **{k: v for k, v in vars(o).items()}}


def serialize_defect(d: Defect) -> dict:
    return {
        "kind": d.kind,
        "channel": d.channel,
        "origin": _serialize_origin(d.origin),
        "finding_refs": [{"ref": r.ref, "role": r.role} for r in d.finding_refs],
        "observation_confidence": d.observation_confidence,
        "attribution_confidence": d.attribution_confidence,
        "propagation": list(d.propagation),
        "base_assumed": d.base_assumed,
        "observability_boundary": d.observability_boundary,
        "unverified_in_channel": d.unverified_in_channel,
        "recovered": d.recovered,
        "quality_unmeasured": d.quality_unmeasured,
    }


def deserialize_origin(o: dict) -> Origin:
    kind = o["kind"]
    if kind == "Localized":
        return Localized(run_id=o["run_id"])
    if kind == "Unlocalized":
        return Unlocalized(reason=o["reason"])
    if kind == "External":
        return External(run_id=o.get("run_id"), reason=o.get("reason", ""))
    if kind == "Design":
        return Design(reason=o["reason"])
    raise ValueError(f"unknown origin kind: {kind!r}")


def _deserialize_ref(r: object) -> FindingRef:
    # Legacy schema-2 payloads stored bare indices; their polarity was never
    # classified, so the honest reconstruction is "context", not "supporting".
    if isinstance(r, int):
        return FindingRef(ref=r, role="context")
    if isinstance(r, dict):
        return FindingRef(ref=int(r["ref"]), role=r.get("role", "context"))
    raise ValueError(f"unknown finding ref shape: {r!r}")


def deserialize_defect(d: dict) -> Defect:
    """Reconstruct a Defect from its serialized form (for derivation/rendering
    over stored schema-2 evidence)."""
    return Defect(
        kind=d["kind"],
        channel=d["channel"],
        origin=deserialize_origin(d["origin"]),
        finding_refs=tuple(_deserialize_ref(r) for r in d.get("finding_refs", ())),
        observation_confidence=d.get("observation_confidence"),
        attribution_confidence=d.get("attribution_confidence"),
        propagation=tuple(d.get("propagation", ())),
        base_assumed=d.get("base_assumed", False),
        observability_boundary=d.get("observability_boundary", False),
        unverified_in_channel=d.get("unverified_in_channel"),
        recovered=d.get("recovered", False),
        quality_unmeasured=d.get("quality_unmeasured", False),
    )
