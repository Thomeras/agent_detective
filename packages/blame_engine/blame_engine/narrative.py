"""Narrative — one template per finding kind / (defect kind, channel) (§2.4).

Prose is a RENDER artifact, generated from the typed Finding/Defect, never stored
as the source of truth and never parsed back. The invariant that makes a whole
class of bugs unrepresentable: a template may interpolate ONLY fields of the
Finding/Defect it renders — there is no free-prose channel from decision code, so
a sentence claiming a defect must, by construction, hold a reference to the
finding that shows it ("TWO independent faults" with zero content candidates
becomes unwritable).

Certainty taxonomy (§2.4, revised): NO channel gets an epistemic title.
Deterministic findings render as "deterministic" (the rule fired reproducibly),
judged as "assessment". "Ground truth" is banned outright: it overclaimed (a
contract reference can itself diverge from the user's requirement — the
docx-vs-'jako PDF' chain), and it collided with the UI's human-feedback label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .defect import (
    REASON_INPUT_ALREADY_FLAWED,
    REASON_NO_CONTENT_CANDIDATE,
    REASON_NO_FORM_VERIFIER,
    REASON_ORCHESTRATION_LAYER,
    Defect,
    Design,
    External,
    Localized,
    Origin,
    Unlocalized,
)
from .finding import Finding


def _channel_word(channel: str) -> str:
    # No epistemic titles: "deterministic" states HOW the fact was measured
    # (reproducible rule), not that it is beyond dispute (§2.4 revised).
    return "deterministic" if channel == "deterministic" else "assessment"


# --- Finding templates ---------------------------------------------------


def render_finding(f: Finding) -> str:
    """Render one Finding to a single sentence using ONLY its own fields."""
    kind = f.kind
    d = f.data
    basis = _channel_word(f.channel)
    if kind == "contract_breach":
        return (
            f"contract breach ({basis}): '{d.get('key')}' carried as "
            f"{d.get('from')!r} arrived rewritten to {d.get('to')!r} at "
            f"{f.subject}"
        )
    if kind == "content_score":
        return f"content score ({basis}): {f.subject} scored {d.get('score')}"
    if kind == "content_flag":
        return (
            f"content flag ({basis}): {f.subject} — {d.get('flag')} "
            f"(judge admitted under-delivery)"
        )
    if kind == "terminal_content":
        state = "bad" if d.get("bad") else "ok"
        return (
            f"terminal content ({basis}): {state} — {d.get('reasoning')!r} "
            f"(score {d.get('score')})"
        )
    if kind == "terminal_form":
        return (
            f"terminal form ({basis}): shipped form does not match the requested "
            f"one (required {d.get('requirement')!r}, observed {d.get('observed')!r})"
        )
    if kind == "verifier_verdict":
        return (
            f"verifier verdict ({basis}): {f.subject} — {d.get('basis')} "
            f"(its PASS/FAIL was wrong)"
        )
    if kind == "loop_anomaly":
        return (
            f"loop anomaly ({basis}): {d.get('iterations')} iterations "
            f"({d.get('limit_kind')})"
        )
    if kind in ("representation_divergence", "requirement_provenance", "assessment_conflict"):
        return (
            f"{kind}: fact '{d.get('fact_key')}' has unreconciled values "
            f"{d.get('values')} reported by {d.get('reported_by')}"
        )
    if kind == "deterministic_signal":
        return (
            f"deterministic signal ({basis}): {d.get('name')} "
            f"({d.get('severity')}) at {f.subject} — {d.get('detail')}"
        )
    if kind == "content_drop":
        return (
            f"content drop ({basis}): {f.subject} scored {d.get('score')} from "
            f"base {d.get('base')} (drop {d.get('drop')})"
        )
    if kind == "input_flawed":
        return (
            f"input flawed ({basis}): {f.subject} reported its own input "
            "already flawed"
        )
    if kind == "detection_gap":
        return (
            f"detection gap ({basis}): no pipeline verifier owns the "
            f"{d.get('dimension')} dimension — "
            f"{origin_reason_phrase(str(d.get('reason') or ''))}"
        )
    if kind == "required_section":
        return (
            f"required section ({basis}): '{d.get('section')}' is "
            f"{d.get('value')} in the {f.subject} representation"
        )
    if kind == "breach_propagated":
        return (
            f"breach propagated ({basis}): rewritten {d.get('key')}="
            f"{d.get('to')!r} verified in the shipped deliverable "
            f"({d.get('basis')})"
        )
    if kind == "breach_corrected":
        return (
            f"breach corrected ({basis}): the deliverable carries the original "
            f"{d.get('key')}={d.get('from')!r} ({d.get('basis')})"
        )
    # Unknown kind: render the fact without inventing interpretation.
    return f"{kind} ({basis}) at {f.subject}"


# --- Defect templates (keyed by kind × channel) --------------------------


# Origin reason CODES → their one phrasing. An Origin carries an identifier, so
# "why localization failed" is queryable and comparable across runs instead of
# being a sentence someone has to diff. An unknown code renders verbatim, which
# is what keeps pre-collapse stored defects (they hold prose) readable.
_ORIGIN_REASONS: dict[str, str] = {
    REASON_ORCHESTRATION_LAYER: (
        "no node individually failed; the fault enters at the "
        "orchestration/task-design layer"
    ),
    REASON_NO_CONTENT_CANDIDATE: (
        "terminal content is bad but no node qualifies as a content origin "
        "(deterministic-only candidate scored healthy, successors recovered)"
    ),
    REASON_INPUT_ALREADY_FLAWED: "input entered the graph already flawed",
    REASON_NO_FORM_VERIFIER: (
        "no verifier charter in this graph covers form/contract vision "
        "(verifier charters cover content)"
    ),
}


def origin_reason_phrase(reason: str) -> str:
    return _ORIGIN_REASONS.get(reason, reason)


def _origin_phrase(origin: Origin) -> str:
    if isinstance(origin, Localized):
        return f"localized at {origin.run_id}"
    if isinstance(origin, Unlocalized):
        return f"observed but not localized ({origin_reason_phrase(origin.reason)})"
    if isinstance(origin, External):
        where = f" at {origin.run_id}" if origin.run_id else ""
        return (
            f"entered from outside the graph{where} "
            f"({origin_reason_phrase(origin.reason)})"
        )
    if isinstance(origin, Design):
        return f"a design-level gap ({origin_reason_phrase(origin.reason)})"
    return "origin unknown"


def render_defect(d: Defect) -> str:
    """Render one Defect to a single sentence from its own typed fields.

    Templates are keyed by (kind, channel); no free prose. Caveat FIELDS render
    as trailing chips so they can never be truncated away mid-sentence (§2.4).
    """
    basis = _channel_word(d.channel)
    origin = _origin_phrase(d.origin)
    if d.kind == "contract":
        head = f"contract defect ({basis}) — {origin}"
    elif d.kind == "content":
        head = f"content defect ({basis}) — {origin}"
    elif d.kind == "form":
        head = f"form defect ({basis}) — {origin}"
    elif d.kind == "loop":
        head = f"loop defect ({basis}) — {origin}"
    elif d.kind == "verification":
        head = f"verification defect ({basis}) — {origin}"
    else:
        head = f"{d.kind} defect ({basis}) — {origin}"

    chips: list[str] = []
    if d.base_assumed:
        chips.append("baseline assumed")
    if d.observability_boundary:
        chips.append("observability boundary")
    if d.unverified_in_channel:
        chips.append(f"unverified in {d.unverified_in_channel}")
    if d.recovered:
        chips.append("recovered downstream")
    if chips:
        head += " [" + " · ".join(chips) + "]"
    return head


# --- Note records: the classification rationale, typed -------------------
#
# The cascade used to write its rationale as inline English f-strings. That was
# the last free-prose channel out of decision code: a sentence could claim
# anything, tests pinned exact substrings, and rewording was a breaking change
# across three layers (§1). A NoteRecord carries the SLUG (the stable identity
# consumers key on) plus the typed data the sentence is made of; the templates
# below are the only place that turns either into English. Decision code can no
# longer say something its own data does not contain.


@dataclass(frozen=True)
class NoteRecord:
    """One classification-rationale fact: a stable slug + its typed payload.

    ``slug`` is the note's identity (it is also the rendered sentence's prefix,
    which is what the legacy renderer and stored reports key on). Variants of one
    slug are distinguished by ``data["variant"]``, never by a second slug — the
    prefix a consumer greps for must not depend on which branch fired.
    """

    slug: str
    data: Mapping[str, Any] = field(default_factory=dict)


def serialize_note(n: NoteRecord) -> dict:
    return {"slug": n.slug, "data": dict(n.data)}


def deserialize_note(d: Mapping[str, Any]) -> NoteRecord:
    return NoteRecord(slug=d["slug"], data=dict(d.get("data") or {}))


def _q(value: Any) -> str:
    """repr() of a payload value, matching the old f-string ``!r`` conversions.

    JSON round-trips ``None`` and strings faithfully, so a record rendered from
    stored evidence produces the same sentence it did at analysis time.
    """
    return repr(value)


def _breach_detail(breaches: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(
        f"{b['agent']} {b['key']}: {_q(b['from'])}->{_q(b['to'])}" for b in breaches
    )


def _shipped_detail(shipped: Sequence[Mapping[str, Any]]) -> str:
    """Verified-shipped contract breaches, with the basis that verified them."""
    return "; ".join(
        f"{r['key']}: {_q(r['from'])}->{_q(r['to'])} ({r['basis']})" for r in shipped
    )


def _violation_detail(violations: Sequence[Mapping[str, Any]], sep: str = ":") -> str:
    return "; ".join(
        f"{v['key']}{sep}{_q(v['from'])}->{_q(v['to'])}" for v in violations
    )


# --- unclassified: WHY each verdict was ruled out ------------------------
# The fallback row states which precondition rejected each candidate verdict.
# Those reasons were prose inside a prose note (a nested free-text channel), so
# they get their own code + payload and their own template table.

_UNCLASSIFIED_REASONS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "no_terminal_verdict": lambda d: (
        "no terminal verdict available (tier1 terminal judge missing or "
        "errored) — composition_failure and fabrication-cascade both "
        "require a checkable terminal assessment"
    ),
    "terminal_not_checkable": lambda d: (
        "terminal verdict not checkable (the judge never saw the "
        "deliverable) — discarded as evidence, so it cannot support "
        "composition_failure or fabrication-cascade"
    ),
    "terminal_ok": lambda d: (
        f"terminal verdict is ok (score={d['score']}) — there is no "
        "terminal failure for a fallback verdict to explain"
    ),
    "hidden_unscored": lambda d: (
        f"genuinely unscored node(s) {d['agents']} could hide the culprit — "
        "blocks composition_failure"
    ),
    "significant_drop_shadowed": lambda d: (
        "a significant drop was observed but every origin was shadowed "
        "or excluded"
    ),
    "unhealthy_not_origin": lambda d: (
        f"below-threshold node(s) {d['agents']} did not qualify as an "
        "origin (inherited/recovered degradation)"
    ),
    "no_failure_signal": lambda d: (
        "all scored nodes healthy and no failure signal to explain "
        "(e.g. a sampled healthy graph)"
    ),
}


def _unclassified(d: Mapping[str, Any]) -> str:
    reasons = "; ".join(
        _UNCLASSIFIED_REASONS[r["code"]](r) for r in d.get("reasons", ())
    )
    return "unclassified: no origin localised — " + reasons


# --- cut_point: one slug, several localization bases ---------------------


def _cut_point(d: Mapping[str, Any]) -> str:
    variant = d.get("variant")
    if variant == "loop":
        tail = (
            f"; the loop's exit '{d['exit_run_id']}' only carried it downstream"
            if d["drilled"]
            else " (which is also the cycle's exit node)"
        )
        return (
            f"cut_point: quality broke at '{d['run_id']}' "
            f"(score={d['score']:.3f}, drop={d['drop']:.3f}) "
            f"inside a {d['members']}-member cycle" + tail
        )
    if variant == "loop_unmeasured":
        return (
            f"cut_point: quality broke at '{d['run_id']}' "
            f"(score={d['score']:.3f}) inside a "
            f"{d['members']}-member cycle; no scored predecessor inside "
            "the cycle, so the drop is not measurable — the localisation "
            "rests on the member's own sub-threshold score at the "
            "observability boundary"
        )
    if variant == "cumulative":
        chain = " -> ".join(
            f"{step['agent']}({step['score']:.2f})" for step in d["chain"]
        )
        return (
            f"cut_point (cumulative degradation): no single step crossed the "
            f"gap threshold ({d['gap_threshold']:.2f}), but quality eroded by "
            f"{d['drop']:.2f} across {chain} — past the cumulative "
            f"threshold ({d['cum_threshold']:.2f}). The erosion starts at "
            f"'{d['run_id']}' (score {d['score']:.2f} from healthy "
            f"base {d['base']:.2f}); review the whole chain, the seed of "
            f"the failure may sit in the last healthy node's output"
        )
    if variant == "deterministic":
        # The judged score is REPORTED, never assumed: this channel localises
        # without the judge, so "no score" is a normal state here (a --no-judge
        # run scores nothing at all) and must read as unjudged, not as 0.00.
        judged = (
            f"judged {d['score']:.2f}, untouched"
            if d["score"] is not None
            else "no judged score on this run — the quality channel produced none"
        )
        return (
            f"cut_point: single unshadowed candidate '{d['run_id']}' "
            "— localised by a DETERMINISTIC check on its own output "
            f"({judged}); origination is "
            "observed from the input/output diff, not a score drop"
        )
    if variant == "base_assumed":
        return (
            f"cut_point: single unshadowed candidate '{d['run_id']}' "
            f"(score={d['score']:.3f}; no scored predecessor — the "
            "1.00 baseline is ASSUMED from a clean handoff, not measured)"
        )
    if variant == "fabrication":
        flags = ", ".join(d["flags"])
        others = f" (also flagged: {d['others']})" if d["others"] else ""
        return (
            "cut_point (fabrication cascade): no score gap, but "
            f"'{d['agent']}' was "
            f"flagged [{flags}] by its own judge and the bad terminal verdict "
            f"corroborates it — terminal evidence: {_q(d['terminal_reasoning'])} "
            f"(tier1 terminal judge, score={d['terminal_score']}). Required "
            "content went missing here first; downstream nodes claimed success "
            "over it" + others
        )
    return (
        f"cut_point: single unshadowed candidate '{d['run_id']}' "
        f"(score={d['score']:.3f}, drop={d['drop']})"
    )


def _degraded_recovered(d: Mapping[str, Any]) -> str:
    detail = _violation_detail(d["violations"])
    if d["via"] == "deterministic":
        # "passed on content" is a claim about a judged score. Without one the
        # node did not pass anything — it was simply never judged.
        content_clause = (
            f"passed on content (judged {d['score']:.2f})"
            if d["score"] is not None
            else "was never judged on content (no quality score)"
        )
        lead = (
            f"degraded_recovered: '{d['agent']}' "
            f"{content_clause} but a "
            "deterministic check failed here"
            + (
                f" — silently violated an input contract ({detail})"
                if d["violations"]
                else ""
            )
        )
    else:
        lead = (
            f"degraded_recovered: '{d['agent']}' "
            f"scored {d['score']:.2f} (below threshold {d['threshold']:.2f})"
            + (
                f" and silently violated an input contract ({detail})"
                if d["violations"]
                else ""
            )
        )
    return (
        lead
        + ", but every successor scored healthy and the terminal "
        f"deliverable is ok (terminal content judge, checkable: {_q(d['terminal_reasoning'])}) — a "
        "near-miss the pipeline compensated for, not a live quality break. "
        "Surfaced as a fragile point to harden, not paged as a broken run"
        + (
            ". CAVEAT: recovery is proven for CONTENT only — the silently "
            "rewritten contract parameter leaves the run unverified in "
            "contract (see contract_vs_terminal); do not treat it as fully "
            "clean"
            if d["violations"]
            else ""
        )
    )


def _verification_gap(d: Mapping[str, Any]) -> str:
    t = d["threshold"]
    parts: list[str] = []
    for g in d["gaps"]:
        name, s = g["agent"], g["score"]
        if g["basis"] == "passed_bad_terminal":
            parts.append(
                f"'{name}' scored healthy ({s:.2f}) yet let the work through "
                "while the terminal output is bad"
            )
        elif g["issued_fail"]:
            parts.append(
                f"'{name}' issued a FAIL the role-aware judge scored wrong "
                f"(score {s:.2f} < threshold {t:.2f}) — a false "
                "alarm the ok terminal contradicts"
            )
        else:
            parts.append(
                f"'{name}' issued a PASS the role-aware judge scored wrong "
                f"(score {s:.2f} < threshold {t:.2f})"
            )
    note = "verification_gap: " + "; ".join(parts) + "."
    if d["terminal"] == "bad":
        note += (
            f" Terminal evidence (bad, checkable assessment): {_q(d['terminal_reasoning'])} "
            "(tier1 terminal judge)."
        )
    elif d["terminal"] == "ok":
        note += (
            f" The terminal verdict is ok (score={d['terminal_score']}) — these are "
            "wrong-FAIL false alarms the checkable terminal contradicts, not "
            "passed-through bad work."
        )
    return note


def _contract_vs_terminal(d: Mapping[str, Any]) -> str:
    detail = _breach_detail(d["breaches"])
    if d["variant"] == "corroborated":
        return (
            f"contract_vs_terminal: the bad terminal verdict cites the "
            f"breached parameter — the contract breach ({detail}) and the "
            "terminal failure describe the same fault (corroborated)"
        )
    if d["variant"] == "independent":
        return (
            f"contract_vs_terminal: a deterministic contract breach "
            f"({detail}) exists AND the terminal verdict is bad — but the "
            "terminal reasoning does not cite the breached parameter, so "
            "these are treated as TWO INDEPENDENT faults sharing an origin "
            "(a content failure does not corroborate a format breach); "
            "remediate both"
        )
    return (
        f"contract_vs_terminal: a deterministic contract breach ({detail}) "
        f"was introduced mid-pipeline, and the terminal CONTENT judge still "
        f"passed the run (score={d['terminal_score']}, {_q(d['terminal_reasoning'])}). "
        "Since the "
        "rubric split the terminal judge is TWO checks: the content rubric "
        "(which verifies substance, not carried contract parameters — its ok "
        "verdict cannot clear the breach) and the form rubric (see "
        "terminal_form, which CAN see the shipped form). Neither is a "
        "pipeline verifier: no verifier charter in this graph owns "
        "form/contract vision, which is why a breach can ship behind an ok "
        "content verdict. Whether the rewritten value propagated into the "
        "final artifact is NOT decidable from node scores alone (see the "
        "contract_propagation note if payload evidence settled it, else "
        "verify out of band) — treat the run as recovered in content but "
        "unverified in contract, not as clean"
    )


_NOTE_TEMPLATES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "no_scores": lambda d: "no_scores: all scores unknown",
    "root_cause_external": lambda d: (
        f"root_cause_external: source candidate '{d['run_id']}' "
        "reports input_flawed=True"
    ),
    "loop_detected": lambda d: (
        f"loop_detected: {d['iterations']} iterations "
        f"({d['limit_kind']}) of agent(s) {d['agents']}"
    ),
    "independent_origins": lambda d: (
        f"independent_origins: the anomalous loop is not the only fault — "
        f"{d['count']} origin(s) outside it "
        f"({d['agents']}) localised "
        "on their own evidence and are reported alongside it"
    ),
    "degraded_recovered": _degraded_recovered,
    "terminal_defect_unlocalized": lambda d: (
        "terminal_defect_unlocalized: the terminal deliverable is bad on "
        f"CONTENT (terminal content judge, checkable: {_q(d['terminal_reasoning'])}), but no node "
        "qualifies as a content origin — the sole candidate "
        f"'{d['agent']}' is a deterministic-"
        f"channel origin ({_violation_detail(d['violations']) or 'deterministic check failure'}) "
        # The judge may never have run on this node (deterministic-only run):
        # "the judge scored it X" must not be asserted from a missing score.
        + (
            "whose own content the judge "
            f"scored {d['score']:.2f} (untouched)"
            if d["score"] is not None
            else "whose own content was never judged (no quality score)"
        )
        + " and whose successors "
        "recovered. The contract fault IS localized here (see "
        "attribution); the content defect observed at the terminal is "
        "NOT localized — do not read the attribution as blame for the "
        "terminal content. Possible sources: a defect below scoring "
        "resolution, a rubric blind spot, or degradation at an unscored "
        "boundary"
    ),
    "cut_point": _cut_point,
    "multi_culprit": lambda d: (
        f"multi_culprit: {d['count']} independent candidates: {d['culprits']}"
    ),
    "composition_failure": lambda d: (
        "composition_failure: no node individually failed (all scores above "
        "threshold, no significant single-edge drops, no cumulative "
        "degradation chain) yet the terminal verdict is bad — terminal "
        f"evidence: {_q(d['terminal_reasoning'])} (tier1 terminal judge, "
        f"score={d['terminal_score']}). "
        "Most likely an orchestration/task-design issue entering at source "
        f"'{d['source']}'"
    ),
    "unclassified": _unclassified,
    "terminal_stale": lambda d: (
        "terminal_stale: the terminal verdict's deterministic basis no "
        "longer reproduces on the current payload/rule set — the tier1 "
        "verdict was computed under a different registered rule set, or "
        "the artifact/payload diverged (representation divergence). Its "
        f"verdict (bad={d['bad']}, score={d['score']}, {_q(d['reasoning'])}) is "
        "treated as UNRELIABLE and discarded as evidence. Re-run the analysis "
        "end-to-end (tier1 included) for a fresh verdict"
    ),
    "terminal_not_checkable": lambda d: (
        "terminal_not_checkable: the terminal judge could not see the final "
        "deliverable (its content was absent from the payload — a file "
        "reference, an orchestrator wrapper, or a verifier verdict rather "
        "than the artifact), so there is NO checkable terminal assessment. Its "
        f"verdict (bad={d['bad']}, score={d['score']}, {_q(d['reasoning'])}) is "
        "discarded — not treated as a failure. Fix the instrumentation to "
        "embed the artifact text if you want a terminal check here"
    ),
    "instrumentation_warning": lambda d: (
        f"instrumentation_warning: node(s) {d['agents']} have no output "
        "payload — they cannot be scored or blamed, which blinds the "
        "analysis; fix the exporter/instrumentation for these nodes"
    ),
    "topology": lambda d: (
        f"topology: graph has {d['components']} weakly-connected "
        "components — runs share membership but lack instrumented edges "
        "between components; blame localisation across components is "
        "impossible. Enable A2A detection or instrument SPAWN/TOOL edges"
    ),
    "verdict_conflict": lambda d: (
        f"verdict_conflict: terminal verdict is bad (tier1 judge: "
        f"{_q(d['terminal_reasoning'])}) yet terminal node "
        f"'{d['agent']}' scored "
        f"{d['score']:.2f} — treating the checkable terminal assessment as "
        "the reference; the healthy score of a verifier that passed bad work "
        "is itself part of the failure (see verification gaps)"
    ),
    "verification_gap": _verification_gap,
    "escalation": lambda d: (
        "escalation: verdict upgraded from degraded_recovered to "
        f"shipped_with_latent_defect — the contract breach ({_shipped_detail(d['shipped'])}) "
        "was VERIFIED in the shipped artifact (see contract_propagation). The "
        "pipeline recovered the CONTENT, and the terminal content judge passed "
        "it; only the detective's own form/propagation checks caught the shipped "
        "breach post-hoc — no pipeline verifier owns contract/form vision, so a "
        "contract-nonconformant deliverable reached production: a silent "
        "failure, not a near-miss"
    ),
    "form_defect_shipped": lambda d: (
        "form_defect_shipped: the terminal FORM dimension is bad"
        + (
            f" (requirement read verbatim from the initial input: "
            f"{_q(d['requirement'])})"
            if d["requirement"]
            else ""
        )
        + (f"; observed: {_q(d['observed'])}" if d["observed"] else "")
        + " — the deliverable shipped in a form other "
        "than the explicitly requested one. No verifier in this graph owns "
        "form/contract vision (verifier charters cover content), so this is "
        "a DESIGN-level gap, not an individual verifier failure: add a "
        "form/contract check to a verifier's charter or register a "
        "deterministic check rule"
    ),
    "requirement_provenance": lambda d: (
        f"requirement_provenance: the deterministic contract "
        f"reference for '{d['key']}' at "
        f"'{d['agent']}' is {_q(d['from'])}, but the "
        f"terminal judge read the user's explicit requirement as "
        f"{_q(d['requirement'])} — the reference value does not "
        f"appear in that quote, so it is NOT user-request-derived "
        f"(harness default or upstream rewrite). The true breach is "
        f"measured against the user requirement; treat "
        f"{_q(d['from'])}->{_q(d['to'])} as scaffold provenance until "
        f"reconciled"
    ),
    "claims_vs_reality": lambda d: (
        f"claims_vs_reality: producer "
        f"'{d['agent']}' scored {d['score']:.2f} ('healthy') "
        "for the very artifact the terminal judge rejected — "
        f"terminal evidence: {_q(d['terminal_reasoning'])}. The node-level score "
        "is overridden as a claim, not accepted as fact"
    ),
    "cascade_participants": lambda d: (
        f"cascade_participants: producer(s) {d['agents']} scored healthy "
        "while building on input flagged for missing required content — "
        "their success claims are unverified against the missing "
        "content, not independent evidence of quality"
    ),
    "contract_vs_terminal": _contract_vs_terminal,
    "attribution_capped": lambda d: (
        "attribution_capped: content_degradation — the origin sits at the "
        "observability boundary (no scored predecessor; the baseline is "
        "assumed, not measured), so attribution of the content defect "
        f"cannot exceed {d['cap']:.2f}. The cap is "
        "specific to inferred defects; a deterministically observed defect "
        "(contract violation) is not subject to it"
    ),
}


def render_note(n: NoteRecord) -> str:
    """Render one NoteRecord to its sentence. Unknown slugs degrade to the slug
    itself rather than inventing interpretation."""
    template = _NOTE_TEMPLATES.get(n.slug)
    return template(n.data) if template is not None else n.slug


def render_notes(records: Sequence[NoteRecord]) -> list[str]:
    return [render_note(n) for n in records]


def find_notes(
    records: Sequence[Mapping[str, Any]], slug: str, **data_match: Any
) -> list[dict]:
    """Serialized note records matching ``slug`` (and every given data field).

    The typed replacement for grepping rendered sentences. A consumer that needs
    to know "did the fabrication-cascade row fire" asks for the record, so a
    reworded template can never silently turn its branch off.
    """
    return [
        dict(r)
        for r in records
        if r.get("slug") == slug
        and all((r.get("data") or {}).get(k) == v for k, v in data_match.items())
    ]


def has_note(
    records: Sequence[Mapping[str, Any]], slug: str, **data_match: Any
) -> bool:
    return bool(find_notes(records, slug, **data_match))


def render_attribution_basis(defect: str, data: Mapping[str, Any]) -> str:
    """The per-defect attribution basis shown in ``attribution_breakdown``.

    Rendered here for the same reason the notes are: it is a SENTENCE about
    evidence, and the numbers it quotes (the boundary cap) must come from the
    same typed payload the number itself came from.
    """
    if defect == "contract_violation":
        return (
            "deterministic: the carried parameter was observed intact "
            f"in the input and rewritten in the output "
            f"({_violation_detail(data['violations'], sep=': ')}) — "
            "origination is observed, not inferred"
        )
    if data["base_assumed"]:
        return (
            "observability boundary — no scored predecessor, the baseline "
            f"is assumed (capped at {data['cap']:.2f})"
        )
    return "measured drop from a scored predecessor"


def render_score_override_reason() -> str:
    """Why a verifier's judged 'verdict correctness' is shown struck through."""
    return (
        "PASS refuted by the checkable terminal assessment — the judged "
        "'verdict correctness' cannot stand (rubber stamp)"
    )


def render_terminal_caveat(breaches: Sequence[Mapping[str, Any]]) -> str:
    """The caveat stamped ON the terminal verdict when a contract breach shipped
    behind an ok CONTENT verdict — same typed inputs as contract_vs_terminal."""
    return (
        f"ok in CONTENT only — a contract breach ({_breach_detail(breaches)}) was "
        "introduced mid-pipeline; conformance of the shipped artifact "
        "to the carried contract is unverified at this level (see "
        "contract_vs_terminal / contract_propagation)"
    )


# --- Candidacy records: the per-node audit trail, typed ------------------
#
# Candidacy answers "why was this node blamed, or not" for EVERY node. It was a
# second prose channel with the same failure mode as notes — the "⚠ refuted"
# label over "the verifier correctly passed" and the "near-miss" line under an
# escalated headline both lived here. Same treatment: a typed verdict code plus
# the numbers the decision rested on, rendered by one template each.


@dataclass(frozen=True)
class CandidacyRecord:
    verdict: str                                  # code, see _CANDIDACY_TEMPLATES
    data: Mapping[str, Any] = field(default_factory=dict)


def serialize_candidacy(c: CandidacyRecord) -> dict:
    return {"verdict": c.verdict, "data": dict(c.data)}


def deserialize_candidacy(d: Mapping[str, Any]) -> CandidacyRecord:
    return CandidacyRecord(verdict=d["verdict"], data=dict(d.get("data") or {}))


def _cand_degraded_recovered(d: Mapping[str, Any]) -> str:
    det = _violation_detail(d["violations"])
    if det:
        # The contract check compares the node's OWN observed input to its
        # output: the parameter demonstrably ARRIVED intact, so the fault
        # demonstrably originated here — attribution rests on observation.
        provenance = (
            "attribution: its input was observed intact (the contract "
            "parameter arrived correctly), so the rewrite demonstrably "
            "originated here"
        )
    elif d["base_assumed"]:
        provenance = (
            "attribution: no scored predecessor — the clean 1.00 baseline "
            "is ASSUMED (structural-root handoff carries no content)"
        )
    else:
        provenance = None
    # A deterministic origin is NOT localised by a sub-threshold score; its
    # judged score is untouched (often healthy). Leading with "score <
    # threshold" would be false — lead with the hard check instead.
    if d["via"] == "deterministic":
        head = (
            "deterministic check FAILED here"
            + (f" — contract violation ({det})" if det else "")
            # "content healthy" is a JUDGED claim. With no score there is no such
            # claim to make — the content channel simply never reported.
            + (
                f"; judged {d['score']:.2f} (content healthy)"
                if d["score"] is not None
                else "; content never judged on this run (no quality score)"
            )
        )
    else:
        head = (
            f"degraded here — score {d['score']:.2f} < threshold {d['threshold']:.2f}"
            + (f", contract violation ({det})" if det else "")
        )
    return (
        head
        + " — but every successor recovered and the terminal is ok; a "
        "near-miss (fragile node), not the origin of a live failure"
        + (f". {provenance}" if provenance else "")
    )


def _cand_below_not_origin(d: Mapping[str, Any]) -> str:
    if d["why"] == "no_predecessor":
        why = "no scored predecessor to measure a break against"
    elif d["why"] == "predecessor_also_low":
        why = (
            f"its best scored predecessor is {d['base']:.2f}, "
            "so quality was not observed to break HERE"
        )
    else:
        why = (
            f"the drop from {d['base']:.2f} stayed under the "
            f"gap threshold {d['gap_threshold']:.2f}"
        )
    return (
        f"score {d['score']:.2f} < threshold {d['threshold']:.2f} — below "
        f"threshold but NOT an origin: {why}. No origin was localised in this "
        "report, so there is nothing upstream this was shadowed by"
    )


_CANDIDACY_TEMPLATES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "composition_suspect": lambda d: (
        "suspect (fallback) — no node individually broke; the "
        "orchestration/task-design layer enters the graph here. "
        "Not a proven culprit"
    ),
    "structural_root": lambda d: (
        "structural root — intentionally unscored (orchestrator "
        "entry point with no output payload); excluded by design, "
        "not a data-quality problem"
    ),
    "unscored": lambda d: (
        f"unscored ({d['reason']}) — excluded: a node without a score can "
        "never be scored-in or -out as culprit. If this is "
        "unexpected, fix the instrumentation (see notes)"
    ),
    "gap_verdict_scored_incorrect": lambda d: (
        f"verification gap — the verifier's own PASS/FAIL was judged "
        f"wrong (score {d['score']:.2f} < threshold {d['threshold']:.2f})"
    ),
    "gap_passed_bad_terminal": lambda d: (
        f"verification gap — scored healthy ({d['score']:.2f} >= "
        f"{d['threshold']:.2f}) yet the terminal verdict is bad: its PASS let "
        "bad work through"
    ),
    "degraded_recovered": _cand_degraded_recovered,
    "origin_escalated": lambda d: (
        "origin (escalated) — a deterministic contract check "
        "FAILED here"
        + (f" (judged {d['score']:.2f}, untouched)" if d["score"] is not None else "")
        + " and the breach was VERIFIED in the shipped "
        f"artifact ({_shipped_detail(d['shipped'])}). Content recovered "
        "downstream and the terminal is ok on CONTENT, but a "
        "contract-nonconformant deliverable reached production "
        "— a silent failure, no longer a near-miss"
    ),
    "origin_fabrication": lambda d: (
        f"origin (fabrication cascade) — own judge flagged "
        f"[{', '.join(d['flags'])}]; blended score {d['score']:.2f} stayed "
        f"above threshold {d['threshold']:.2f}, but the bad terminal verdict "
        "corroborates the missing content, and downstream nodes claimed "
        "success over it"
    ),
    "origin_deterministic": lambda d: (
        "origin — a DETERMINISTIC check failed on this node's own "
        "output"
        + (
            f" (contract violation {_violation_detail(d['violations'])})"
            if d["violations"]
            else ""
        )
        # The judged score is context, not the basis. On a --no-judge run there
        # is none, and saying "judged 0.00" there would invent a verdict the
        # judge never issued.
        + (
            f"; judged {d['score']:.2f} untouched"
            if d["score"] is not None
            else "; this node was never judged (no quality score on this run)"
        )
        + " — origination is observed from "
        "the input/output diff, not inferred from a score"
    ),
    "origin_cumulative": lambda d: (
        f"origin — erosion starts here: cumulative drop "
        f"{d['drop']:.2f} from healthy base {d['base']:.2f} across "
        f"{' -> '.join(d['path'])} (cumulative threshold "
        f"{d['cum_threshold']:.2f})"
    ),
    "origin_drop": lambda d: (
        f"origin — score {d['score']:.2f}, dropped {d['drop']:.2f} from its "
        f"best scored predecessor (gap threshold "
        f"{d['gap_threshold']:.2f}, node threshold {d['threshold']:.2f})"
        # "Quality was fine going in" is measured over the inputs we could
        # score. Naming the ones we could not is the difference between an
        # observed handoff and an assumed one.
        + (
            ". CAVEAT: input(s) from "
            f"{d['unmeasured_inputs']} "
            "were never scored, so the handoff into this node is only "
            "partly observed"
            if d["unmeasured_inputs"]
            else ""
        )
    ),
    "origin_vs_predecessor": lambda d: (
        f"origin — score {d['score']:.2f} vs best scored predecessor "
        f"{d['base']:.2f} (drop {d['drop']:.2f}, threshold "
        f"{d['threshold']:.2f})"
    ),
    "origin_boundary": lambda d: (
        f"origin — score {d['score']:.2f} < threshold {d['threshold']:.2f} at "
        "the observable boundary (genuinely no scored predecessor)"
    ),
    "origin_by_classification": lambda d: (
        f"origin — score {d['score']:.2f}; selected by classification, see "
        "notes for the evidence"
    ),
    "loop_member": lambda d: (
        f"loop member — score {d['score']:.2f}, same cycle as the origin; "
        "blame drilled into the worst member"
    ),
    "inherited": lambda d: (
        f"score {d['score']:.2f} < threshold {d['threshold']:.2f} — inherited "
        "degradation, shadowed by the origin upstream"
    ),
    "independent_low": lambda d: (
        f"score {d['score']:.2f} < threshold {d['threshold']:.2f} — degraded, "
        "but not downstream of any localised origin: an independent low that "
        "did not itself qualify as one (see notes)"
    ),
    "below_not_origin": _cand_below_not_origin,
    "degradation_path_start": lambda d: (
        f"degradation-path start — last healthy node ({d['score']:.2f} >= "
        f"{d['threshold']:.2f}) before the erosion ({' -> '.join(d['path'])}, "
        f"cumulative -{d['cumulative_drop']:.2f}); its output may carry the "
        "seed of the failure and is worth manual review"
    ),
    "degradation_path_member": lambda d: (
        f"on the degradation path ({' -> '.join(d['path'])}) — score "
        f"{d['score']:.2f} still >= threshold {d['threshold']:.2f}, part of a "
        f"cumulative -{d['cumulative_drop']:.2f} erosion"
    ),
    "whistleblower": lambda d: (
        f"honest whistle-blower — issued FAIL on the bad work (score "
        f"{d['score']:.2f} = verdict correctness); not a gap"
    ),
    "claims_conflict": lambda d: (
        f"claims-vs-reality conflict — scored {d['score']:.2f} ('healthy') for "
        "the very artifact the terminal judge rejected; the healthy "
        "score is treated as an unverified claim, not as fact"
    ),
    "cascade_participant": lambda d: (
        f"fabrication-cascade participant — scored {d['score']:.2f}, but built "
        "on input flagged for missing required content and claimed "
        "success over it; the score is an unverified claim, not "
        "independent evidence"
    ),
    "transient_low": lambda d: (
        f"dropped {d['drop']:.2f} to {d['score']:.2f} but downstream recovered "
        "— transient low, not a spreading origin"
    ),
    "healthy": lambda d: (
        f"healthy — score {d['score']:.2f} >= threshold {d['threshold']:.2f}"
    ),
}


def render_candidacy(c: CandidacyRecord) -> str:
    template = _CANDIDACY_TEMPLATES.get(c.verdict)
    return template(c.data) if template is not None else c.verdict
