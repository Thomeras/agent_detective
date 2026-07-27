"""Projection: derive_report_type + reconcile (verdict refactor §2.3 / §2.4).

``derive_report_type`` is the ONLY place in the system that knows the
defects → report_type mapping (§3 table, encoded as data). It reads the typed
``Defect[]`` — never strings, never re-deciding the outcome — so a new failure
kind is one new row here instead of ~10 files across three layers.

``reconcile`` is the mandatory fact-identity pass (§2.4): two findings that
share a ``fact_key`` but disagree on their value MUST produce a ``divergence``
finding, so a report can never print two unreconciled versions of one fact.
"""

from __future__ import annotations

from collections.abc import Sequence

from .defect import Defect, External, Localized, Unlocalized
from .finding import PROV_RECONCILE_FACT_KEY, Finding, HarnessState
from .narrative import NoteRecord, render_notes


# --- report_type derivation (§3 table, as data) --------------------------


def _localized_run_ids(defects: Sequence[Defect], kinds: set[str]) -> set[str]:
    out: set[str] = set()
    for d in defects:
        if d.kind in kinds and isinstance(d.origin, Localized):
            out.add(d.origin.run_id)
    return out


def derive_report_type(defects: Sequence[Defect]) -> str:
    """Map a set of typed defects to today's report_type literal.

    The mapping is the §3 table verbatim. Precedence follows the old cascade so
    the projection reproduces every golden fixture. ``form`` defects (Design
    origin) and ``verification`` secondary evidence never change the PRIMARY
    type — they contribute incidents downstream, handled elsewhere.
    """
    # Primary defects are the ones that decide the outcome. Form defects are a
    # design-level annotation (they add a latent_defect incident, never a type).
    primary = [d for d in defects if d.kind in {"content", "contract", "loop"}]
    verification = [d for d in defects if d.kind == "verification"]

    if not primary and not verification:
        return "unclassified"

    # Row 2 — the fault entered from outside the observed graph.
    if any(isinstance(d.origin, External) for d in defects):
        return "root_cause_external"

    # Row 5 — two or more INDEPENDENT localized origins. Checked BEFORE the loop
    # row: a loop defect alongside an independent origin elsewhere is two faults,
    # and answering "loop_detected" would name only one of them. With the loop as
    # the sole origin the set has one member and the loop row below still wins.
    if len(_localized_run_ids(primary, {"content", "contract", "loop"})) >= 2:
        return "multi_culprit"

    # Row 3 — a localized loop anomaly.
    if any(d.kind == "loop" and isinstance(d.origin, Localized) for d in primary):
        return "loop_detected"

    content = [d for d in primary if d.kind == "content"]
    contract = [d for d in primary if d.kind == "contract"]
    # A RECOVERED content defect is a near-miss (fragile node the pipeline
    # compensated for), never a live break — it keeps its origin but must not
    # derive a cut_point (§3 content-only degraded_recovered row).
    content_active = [d for d in content if not d.recovered]
    content_localized = any(isinstance(d.origin, Localized) for d in content_active)
    contract_localized = any(isinstance(d.origin, Localized) for d in contract)

    # Row 4c / fabrication cascade — a localized ACTIVE content origin.
    if content_localized:
        return "cut_point"

    # Row 4a (content-only variant, §3) — every content defect recovered, no
    # contract break: a degraded-but-recovered node.
    if content and not content_active and not contract:
        return "degraded_recovered"

    # Row 4b — contract localized, terminal content bad, content has NO
    # candidate (Unlocalized): the content defect is observed-but-unlocalized.
    if contract_localized and any(isinstance(d.origin, Unlocalized) for d in content_active):
        return "terminal_defect_unlocalized"

    # Row 4a — contract localized, terminal content ok (⇒ no active content
    # defect emitted): a degraded-but-recovered node.
    #
    # "Recovered" is an assertion about the CONTENT channel: successors scored
    # healthy and a checkable terminal said the deliverable is fine. When that
    # channel measured nothing at all (`quality_unmeasured` — a --no-judge run,
    # every node unscored), the assertion has no evidence behind it. All that was
    # observed is a hard, point-attributable breach with no sign of recovery, so
    # the honest projection is an active localized origin. Reporting
    # "degraded_recovered" (rendered "PASSED — with warnings") there turns the
    # absence of a quality measurement into a claim that quality was fine.
    if contract_localized and not content_active:
        if any(d.quality_unmeasured for d in contract):
            return "cut_point"
        return "degraded_recovered"

    # Row 6 — content observed at the terminal, unlocalizable, nothing else
    # localized: composition failure (blame the orchestrator).
    if content_active and all(isinstance(d.origin, Unlocalized) for d in content_active):
        return "composition_failure"

    # Verification gap — verifier verdicts wrong, nothing else localized.
    if verification:
        return "verification_gap"

    return "unclassified"


# --- escalation + incident derivation (§2.3: one home for the mapping) ----

# Report types that are a QUALITY incident (the flagship silent hallucination
# becomes degraded_quality, not a terminal failure). Single source of truth —
# the worker imports this instead of keeping its own copy.
QUALITY_REPORT_TYPES = frozenset(
    {
        "cut_point",
        "multi_culprit",
        "composition_failure",
        "root_cause_external",
        "verification_gap",
        "degraded_recovered",
        # A content-bad terminal whose origin is not localized is still a
        # quality incident — observed ground truth even with no node blamed.
        "terminal_defect_unlocalized",
    }
)

# Deterministic-flag names the incident mapping reads. Kept as literals here so
# the engine has no dependency on the worker's types module; the worker passes
# its flags list (the same strings) in.
_FLAG_LOOP_ANOMALY = "loop_anomaly"
_FLAG_FAILED_RUNS = "failed_runs"
_FLAG_TERMINAL_FORM = "terminal_form_breach"
_FLAG_COST_OVERRUN = "cost_overrun"


def derive_escalation_records(
    report_type: str, contract_results: Sequence[dict]
) -> tuple[str, list[NoteRecord]]:
    """``degraded_recovered`` + a contract breach VERIFIED (status 'propagated')
    in the shipped deliverable escalates to ``shipped_with_latent_defect``.

    Reads the DETERMINISTIC contract_propagation results ONLY — never successor
    scores, never a judge. The single home of the escalation rule (§2.3); the
    worker delegates here. Returns the effective report type and any escalation
    NOTE RECORDS (empty when nothing escalated) — the sentence is the narrative
    layer's job, so this rule cannot narrate more than its own evidence.
    """
    shipped = [r for r in contract_results if r["status"] == "propagated"]
    if not (shipped and report_type == "degraded_recovered"):
        return report_type, []
    return "shipped_with_latent_defect", [
        NoteRecord(
            "escalation",
            {
                "shipped": [
                    {
                        "key": r["key"],
                        "from": r["from"],
                        "to": r["to"],
                        "basis": r["basis"],
                    }
                    for r in shipped
                ]
            },
        )
    ]


def derive_escalation(
    report_type: str, contract_results: Sequence[dict]
) -> tuple[str, list[str]]:
    """Rendered form of :func:`derive_escalation_records` (worker-facing)."""
    rt, records = derive_escalation_records(report_type, contract_results)
    return rt, render_notes(records)


def derive_incident(
    report_type: str, flags: Sequence[str], terminal_bad: bool
) -> tuple[str | None, str | None]:
    """Map an (effective) report type + deterministic flags to an
    ``(incident_key, trigger)``. Blame classification wins for quality issues.

    The single home of the incident mapping (§2.3); the worker delegates here.
    Byte-for-byte the old worker behaviour. ``(None, None)`` when nothing pages.
    """
    if report_type == "loop_detected" or _FLAG_LOOP_ANOMALY in flags:
        return "loop_detected", "loop_detected"
    if report_type == "shipped_with_latent_defect":
        # A VERIFIED contract breach in the shipped deliverable: a silent failure
        # reached production behind an ok terminal. Its own high-severity trigger.
        return "latent_defect", "latent_defect"
    if report_type in QUALITY_REPORT_TYPES:
        return "degraded_quality", "degraded_quality"
    if _FLAG_FAILED_RUNS in flags:
        return "terminal_failure", "terminal_failure"
    if _FLAG_TERMINAL_FORM in flags:
        # Rubric split: the deliverable visibly shipped in a form other than the
        # requested one while nothing else localised — same silent-failure family
        # as a verified contract breach.
        return "latent_defect", "latent_defect"
    if _FLAG_COST_OVERRUN in flags:
        return "cost_overrun", "cost_overrun"
    if terminal_bad:
        return "terminal_failure", "terminal_failure"
    return None, None


# --- reconcile (§2.4 fact_key) -------------------------------------------

# The reconcile-output kinds (what the UI lists as reconcile_divergences).
DIVERGENCE_KINDS = frozenset(
    {"representation_divergence", "requirement_provenance", "assessment_conflict"}
)

# Which divergence kind a conflicting fact_key produces. The prefix of the
# fact_key names the family; the payload is compared on its "value".
_DIVERGENCE_KIND = {
    "contract": "requirement_provenance",
    "required_section": "representation_divergence",
    "file_type_requirement": "requirement_provenance",
    "assessment": "assessment_conflict",
}


def _divergence_kind_for(fact_key: str) -> str:
    family = fact_key.split(":", 1)[0]
    return _DIVERGENCE_KIND.get(family, "representation_divergence")


def reconcile(findings: Sequence[Finding]) -> list[Finding]:
    """Mandatory reconcile pass. Group findings by ``fact_key``; any group whose
    members disagree on ``data["value"]`` emits ONE divergence finding recording
    the conflicting values and the run subjects that reported them.

    A report may never print two versions of one fact without a reconcile verdict
    between them — this is the mechanism that kills the class (§2.4, §11 rows
    1–3). Returns the divergence findings only (the caller appends them).
    """
    by_key: dict[str, list[tuple[int, Finding]]] = {}
    for i, f in enumerate(findings):
        if f.fact_key is None or "value" not in f.data:
            continue
        by_key.setdefault(f.fact_key, []).append((i, f))

    divergences: list[Finding] = []
    for fact_key, members in by_key.items():
        values = {str(f.data.get("value")) for _i, f in members}
        if len(values) <= 1:
            continue  # all agree — nothing to reconcile
        divergences.append(
            Finding(
                kind=_divergence_kind_for(fact_key),
                channel="deterministic",
                subject="graph",
                data={
                    "fact_key": fact_key,
                    "values": sorted(values),
                    "reported_by": [f.subject for _i, f in members],
                    "finding_refs": [i for i, _f in members],
                },
                provenance=HarnessState(detail=PROV_RECONCILE_FACT_KEY),
                certainty=1.0,
                fact_key=fact_key,
            )
        )
    return divergences
