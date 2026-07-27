"""Presentation semantics for the terminal: verdict and defect labels.

The Python twin of ``web/src/verdict/descriptor.ts``. Both render the same
typed verdict for different surfaces, and they must not drift — a terminal that
called a run FAILED where the UI said PASSED would make the two views of one
analysis contradict each other.

Two things keep them honest. Wording lives here as data (never derived from
note strings — rewording a note must never change a verdict label), and
``tests/test_descriptor.py`` pins coverage against ``blame_engine``'s own
``ReportType`` and defect kinds, so a report type added to the engine fails the
suite here instead of silently rendering as UNANALYSED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

Tone = Literal["ok", "warn", "fail", "unknown"]


@dataclass(frozen=True)
class Descriptor:
    """A label, a tone, and a sentence describing the thing."""

    label: str
    tone: Tone
    template: str


# --------------------------------------------------------------------------
# Origins (the §2.2 sum type) — one place turns an origin into prose.
# --------------------------------------------------------------------------

_ORIGIN_TONE: dict[str, Tone] = {
    # A localized fault is a real failure; the same defect left unattributed is
    # a warning at most — we observed it but could not say who caused it.
    "Localized": "fail",
    "External": "warn",
    "Unlocalized": "unknown",
    "Design": "warn",
}


def origin_tone(kind: str) -> Tone:
    return _ORIGIN_TONE.get(kind, "unknown")


def origin_phrase(
    origin: dict[str, Any], run_label: Callable[[str], str] | None = None
) -> str:
    """A short noun phrase for where a defect sits."""
    kind = origin.get("kind")
    if kind == "Localized":
        run_id = origin.get("run_id", "")
        return run_label(run_id) if run_label else run_id
    if kind == "Unlocalized":
        # `reason` is a CODE, not a sentence — printed as-is so the reader sees
        # the same token the JSON carries.
        return f"origin not localized ({origin.get('reason', 'unknown')})"
    if kind == "External":
        return "an external / upstream input"
    if kind == "Design":
        reason = origin.get("reason") or ""
        if reason:
            return f"the graph's design ({reason})"
        return "the graph's design — no node owns the check"
    return "an unrecognised origin"


# --------------------------------------------------------------------------
# Defects
# --------------------------------------------------------------------------

DEFECT_KIND_META: dict[str, dict[str, str]] = {
    "contract": {
        "noun": "Contract breach",
        "why": "A carried input/output parameter was silently rewritten",
    },
    "content": {
        "noun": "Content defect",
        "why": "The deliverable's substance is wrong or missing",
    },
    "form": {
        "noun": "Form defect",
        "why": "The wrong artifact form was shipped (extension/kind mismatch)",
    },
    "loop": {
        "noun": "Runaway loop",
        "why": "Iterations ran past the expected limit",
    },
    "verification": {
        "noun": "Verification gap",
        "why": "A verifier passed work it should have failed",
    },
}


def defect_descriptor(kind: str, origin: dict[str, Any]) -> Descriptor:
    meta = DEFECT_KIND_META.get(
        kind, {"noun": f"{kind.capitalize()} defect", "why": "A defect was recorded"}
    )
    return Descriptor(
        label=meta["noun"],
        tone=origin_tone(str(origin.get("kind"))),
        template=f"{meta['why']} at {{origin}}.",
    )


# --------------------------------------------------------------------------
# Run-level verdicts
# --------------------------------------------------------------------------

VERDICT_META: dict[str, Descriptor] = {
    "cut_point": Descriptor(
        "FAILED", "fail", "Quality demonstrably broke at a localized origin."
    ),
    "multi_culprit": Descriptor(
        "FAILED", "fail", "Several independent origins broke quality."
    ),
    "composition_failure": Descriptor(
        "FAILED",
        "fail",
        "No single node broke — the orchestration / task design is suspected.",
    ),
    "loop_detected": Descriptor(
        "FAILED", "fail", "A runaway loop burned iterations past the limit."
    ),
    "root_cause_external": Descriptor(
        "FAILED", "warn", "The fault entered from outside the observed graph."
    ),
    "verification_gap": Descriptor(
        "FAILED", "fail", "A verifier passed work it should have failed."
    ),
    "shipped_with_latent_defect": Descriptor(
        "LATENT DEFECT",
        "fail",
        "A verified contract breach shipped in the deliverable. The terminal "
        "content judge passed it (the content rubric cannot see carried "
        "contract parameters); the form/propagation checks caught it post-hoc "
        "— no pipeline verifier owns contract/form vision.",
    ),
    "terminal_defect_unlocalized": Descriptor(
        "FAILED",
        "warn",
        "The terminal content is bad, but no node qualifies as a content "
        "origin — only a contract fault is localized. The content defect's "
        "source is unknown.",
    ),
    "degraded_recovered": Descriptor(
        "PASSED — with warnings",
        "warn",
        "A node underperformed, but every downstream step recovered and the "
        "terminal deliverable is ok — a near-miss, not an outage.",
    ),
    "unclassified": Descriptor(
        "INCONCLUSIVE", "unknown", "No failure was localised."
    ),
}

# Analysis never ran for this graph — a first-class state, distinct from an
# analysis that ran and could not localise anything.
UNANALYSED_VERDICT = Descriptor(
    "UNANALYSED", "unknown", "This run has not been analysed yet."
)

# Analysed, no defect localised, no incident raised.
PASSED_VERDICT = Descriptor(
    "PASSED", "ok", "The run completed cleanly — no defect was detected."
)

# Analysis ran but had nothing to measure: no node could be scored, no
# deterministic rule fired, and no checkable terminal verdict was obtained.
# This is NOT a pass. Rendering it as one would assert quality from silence,
# which is the failure mode the whole project is built against — so it gets its
# own verdict rather than being folded into PASSED or INCONCLUSIVE (the latter
# means the analysis measured things and could not localise; this one never
# measured anything).
NOT_VERIFIED_VERDICT = Descriptor(
    "NOT VERIFIED",
    "unknown",
    "Nothing could be measured on this run — this is an absence of evidence, "
    "not evidence of correctness.",
)


def verdict_descriptor(report_type: str | None) -> Descriptor:
    if report_type is None:
        return UNANALYSED_VERDICT
    return VERDICT_META.get(report_type, UNANALYSED_VERDICT)


CULPRIT_HEADING: dict[str, str] = {
    "composition_failure": "Most likely: orchestration / design issue",
    "root_cause_external": "Upstream / external cause",
    "verification_gap": "Rubber-stamping verifier",
    "cut_point": "Origin — where quality broke",
    "degraded_recovered": "Fragile node — degraded but recovered",
    "shipped_with_latent_defect": "Origin — silent defect shipped",
    "terminal_defect_unlocalized": (
        "Contract origin — terminal content defect not localized"
    ),
    "multi_culprit": "Independent origins",
    "loop_detected": "Loop origin",
    "unclassified": "Possible suspect",
}


def culprit_heading(report_type: str | None, plural: bool = False) -> str:
    base = CULPRIT_HEADING.get(report_type or "", "Suspected culprit")
    if not plural:
        return base
    for singular, replacement in (
        ("verifier", "verifiers"),
        ("node", "nodes"),
        ("suspect", "suspects"),
        ("origin", "origins"),
    ):
        if singular in base:
            return base.replace(singular, replacement, 1)
    return base


# Human labels for the structured caveat fields carried on every defect.
CAVEAT_LABELS: dict[str, str] = {
    "base_assumed": "healthy baseline assumed, not observed",
    "observability_boundary": "limited by what the trace captured",
    "unverified_in_channel": "not verified in this evidence channel",
    "recovered": "downstream recovered from it",
}
