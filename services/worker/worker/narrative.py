"""Worker-side narrative templates (verdict refactor §2.4).

The engine collapsed its inline note prose into typed ``NoteRecord``s rendered by
``blame_engine.narrative``; the worker appends notes to the SAME stream, so it
gets the same discipline in its own module. Worker facts (contract propagation
against the deliverable payload, evidence reconciliation, stale-verdict cause)
stay here — they are not engine facts — but the rule is identical: decision code
emits a record, exactly one template turns it into English, and nothing parses
the sentence back.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from blame_engine import NoteRecord


def _propagation_basis(d: Mapping[str, Any]) -> str:
    if d["basis_kind"] == "param":
        return "contract param match"
    if d["basis_kind"] == "path":
        return f"artifact path {d['path']!r} ends '.{d['ext']}'"
    return "nothing observable — no matching contract param or artifact path"


def _contract_propagation(d: Mapping[str, Any]) -> str:
    basis = _propagation_basis(d)
    if d["status"] == "propagated":
        # "recovered in content" is a claim ABOUT CONTENT, and it is only
        # available when the content channel actually ran. On a --no-judge run
        # every node is unscored and nothing measured recovery — asserting it
        # from silence is exactly the fabrication this product exists to refuse.
        # The propagation itself stays verified either way: it was read off the
        # deliverable payload, not inferred from scores.
        aftermath = (
            "the run is recovered in content but shipped with a violated "
            "contract (latent defect)."
            if d.get("content_measured", True)
            else "content was never measured on this run (the quality channel "
            "produced no score), so whether the substance is otherwise sound is "
            "UNKNOWN — what is established is that a violated contract shipped."
        )
        return (
            f"contract_propagation: rewritten {d['key']}={d['to']!r} observed in "
            f"the deliverable producer '{d['agent']}' payload "
            f"(basis: {basis}) — the breach PROPAGATED into "
            f"the shipped artifact (verified); {aftermath}"
        )
    if d["status"] == "corrected":
        return (
            f"contract_propagation: the deliverable producer "
            f"'{d['agent']}' payload carries the ORIGINAL "
            f"{d['key']}={d['from']!r} (basis: {basis}) — the "
            f"breach was corrected downstream; contract restored (verified)."
        )
    return (
        f"contract_propagation: the rewritten {d['key']} is not observable in "
        f"the deliverable payload (basis: {basis}) — propagation "
        f"UNVERIFIED; verify the final artifact's format/contract out of "
        f"band."
    )


_NOTE_TEMPLATES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "contract_propagation": _contract_propagation,
    "evidence_tension": lambda d: (
        "evidence_tension: claims from the origin were found in the final "
        "producer's payload although the verdict says required content went "
        "missing — the evidence streams disagree on where the content was "
        "lost, so the origin is not settled"
    ),
    "representation_divergence": lambda d: (
        "representation_divergence: the terminal output is empty/degenerate "
        "while verifiers reviewed a non-empty artifact — they saw a "
        "different representation than the terminal judge; the content was "
        "likely lost between the last producer and the terminal output "
        "(check the render/export step), and the true origin may be later "
        "than reported"
    ),
    "competing_origins": lambda d: (
        "competing_origins: the origin is NOT settled — confidence is split "
        f"across '{d['origin_agent']}' "
        f"(weight {d['origin_weight']}) and the later producer "
        f"'{d['alt_agent']}' "
        f"(weight {d['alt_weight']}), with {d['unresolved']} unresolved; the "
        "headline confidence is lowered to the dominant hypothesis's share "
        f"({d['origin_weight']}) rather than asserting one origin over two live "
        "hypotheses"
    ),
    "terminal_stale_cause": lambda d: f"terminal_stale_cause: {d['cause']}",
}


def render_note(n: NoteRecord) -> str:
    template = _NOTE_TEMPLATES.get(n.slug)
    return template(n.data) if template is not None else n.slug


def render_notes(records) -> list[str]:
    return [render_note(n) for n in records]


# --- Deterministic signals: typed identity + rendered evidence -----------
#
# A signal is `{name, severity, detail, basis}`; `detail` and `basis` were
# assembled by each detector as prose. That is the same disease one layer down:
# the two strings could disagree with each other or with the values they quote
# (the artifact_integrity size/nonempty pair nearly did — a "size=5000 < min 64"
# basis on a zero-content file would have been a false statement IN the
# evidence). Detectors now emit a CODE plus its params; the two templates below
# are the only place either string is written, from one payload, so they cannot
# drift apart.


def _p(v: object) -> str:
    """`%g`-style number, or the value as-is (params survive JSON)."""
    return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


_SIGNAL_TEMPLATES: dict[str, tuple[Callable[..., str], Callable[..., str]]] = {
    # --- artifact integrity (signals.py) ---
    "artifact_missing": (
        lambda d: f"declared artifact {d['path']} does not exist",
        lambda d: "file missing at flush",
    ),
    "artifact_kind_mismatch": (
        lambda d: f"declared .{d['ext']} but content is {d['detected']}",
        lambda d: f"magic bytes: detected_kind={d['detected']} for {d['path']}",
    ),
    "artifact_parse_failed": (
        lambda d: (
            f"{d['path']} does not parse as a valid "
            f"{('.' + d['ext']) if d.get('ext') else 'artifact'} file"
        ),
        lambda d: "parse check",
    ),
    "artifact_empty": (
        lambda d: f"{d['path']} has no content",
        lambda d: (
            "content check (nonempty=false, size="
            f"{d['size'] if d.get('size') is not None else 'unknown'})"
        ),
    ),
    "artifact_too_small": (
        lambda d: f"{d['path']} is below the minimum plausible size",
        lambda d: f"size check (size={d['size']} < min {d['min_bytes']})",
    ),
    # --- numeric fidelity (checks_numeric.py) ---
    "numeric_content_lost": (
        lambda d: (
            f"input carried {d['input_numbers']} figures, the output carries none"
        ),
        lambda d: (
            f"no numeric token in {d['output_chars']} chars of output; "
            f"{d['input_numbers']} in the input"
        ),
    ),
    "number_not_derivable": (
        lambda d: (
            f"{d['count']} figure(s) trace back to nothing in the input: {d['values']}"
        ),
        lambda d: (
            f"{d['count']} of {d['checked']} tabular figures are neither present in "
            "the input nor reachable from one by a rate the input states"
        ),
    ),
    # --- behavioral (behavioral.py) ---
    "empty_output_with_spend": (
        lambda d: (
            f"produced no output while spending {d['tokens_out']} output tokens"
        ),
        lambda d: (
            f"output.value recorded and empty ({d['chars']} chars); "
            f"gen_ai.usage.output_tokens={d['tokens_out']}"
        ),
    ),
    "run_failed": (
        lambda d: (
            f"the run is recorded as {d['status']}"
            + (f" ({d['spans']} error span(s))" if d.get("spans") else "")
        ),
        lambda d: "run status from the trace",
    ),
    "loop_fingerprint": (
        lambda d: (
            f"tool '{d['tool']}' called {d['calls']}x consecutively "
            "with identical args"
        ),
        lambda d: f"args_sha {d['args_sha']} repeated",
    ),
    "retry_storm": (
        lambda d: (
            f"tool '{d['tool']}' called {d['calls']}x with identical args, "
            f"{d['errors']} with status=error"
        ),
        lambda d: f"args_sha {d['args_sha']}; retry threshold {d['threshold']}",
    ),
    "duplicate_side_effect": (
        lambda d: (
            f"side-effecting tool '{d['tool']}' executed {d['calls']}x "
            "with identical args"
        ),
        lambda d: (
            f"args_sha {d['args_sha']}; all {d['calls']} calls status=ok; "
            f"name matches side-effect marker '{d['marker']}'"
        ),
    ),
    "tool_args_invalid": (
        lambda d: (
            f"tool '{d['tool']}' called with args violating "
            "the registered schema"
        ),
        lambda d: (
            f"registered tool_schema for '{d['tool']}' "
            "(type/required/properties subset check)"
        ),
    ),
    "metric_outlier": (
        lambda d: (
            f"{d['metric']}={_p(d['value'])} is {d['z']:.1f}σ above "
            f"the rolling mean {_p(d['mean'])}"
        ),
        lambda d: (
            f"baseline n={d['sample_count']}, mean={_p(d['mean'])}, "
            f"std={_p(d['std'])}"
        ),
    ),
    # --- content checks (checks_content.py) ---
    "required_section_missing": (
        lambda d: f"required section '{d['section']}' not found",
        lambda d: f"{d['match_kind']} '{d['pattern']}' not present in {d['subject']}",
    ),
    "sum_invariant_breach": (
        lambda d: (
            f"sum({d['items_path']})={_p(d['total_sum'])} != "
            f"{d['total_path']}={_p(d['total'])}"
        ),
        lambda d: (
            f"registered invariant '{d['rule_name']}', "
            f"tolerance {_p(d['tolerance'])}"
        ),
    ),
    "currency_family_mismatch": (
        lambda d: (
            f"input amounts are in {d['input_family']} but output uses "
            f"{d['output_families']}"
        ),
        lambda d: (
            f"currency token match: input family {d['input_family']}, "
            f"output family {d['output_families']}; {d['input_family']} "
            "absent from output"
        ),
    ),
    "date_order_violated": (
        lambda d: (
            f"'{d['end_key']}'={d['end']} is before "
            f"'{d['start_key']}'={d['start']}"
        ),
        lambda d: (
            "ISO date comparison of sibling keys "
            f"'{d['start_key']}'/'{d['end_key']}'"
        ),
    ),
    "deadline_in_past": (
        lambda d: f"'{d['key']}'={d['date']} deadline in the past",
        lambda d: f"run started {d['run_date']}",
    ),
    # --- security checks (checks_security.py) ---
    "sensitive_data": (
        lambda d: f"{d['kind']} detected",
        lambda d: (
            f"{d['kind']} pattern match; value REDACTED "
            f"(first 4 chars '{d['prefix']}…')"
        ),
    ),
    "prompt_injection": (
        lambda d: f"injection signature '{d['signature']}' present",
        lambda d: "literal/unicode pattern match",
    ),
    # --- propagation (tier2.py) ---
    "structured_field_drop": (
        lambda d: (
            f"field value {d['claim']!r} from the origin's output appears in "
            "no downstream payload"
        ),
        lambda d: (
            f"exact normalized match over {d['checked']} checkable downstream "
            "payload(s)"
        ),
    ),
}


def render_signal_detail(code: str, params: Mapping[str, Any]) -> str:
    tpl = _SIGNAL_TEMPLATES.get(code)
    return tpl[0](params) if tpl is not None else code


def render_signal_basis(code: str, params: Mapping[str, Any]) -> str:
    tpl = _SIGNAL_TEMPLATES.get(code)
    return tpl[1](params) if tpl is not None else code


def signal(name: str, severity: str, code: str, **params: Any) -> dict:
    """Build one deterministic signal from its typed identity.

    Returns the record (``name``/``severity``/``code``/``params``) WITH the
    rendered ``detail`` and ``basis`` carried alongside, so stored reports and
    the UI keep the shape they had while every consumer that needs to branch can
    read ``code``/``params`` instead of matching a sentence.
    """
    return {
        "name": name,
        "severity": severity,
        "code": code,
        "params": dict(params),
        "detail": render_signal_detail(code, params),
        "basis": render_signal_basis(code, params),
    }


# --- terminal-verdict display fields -------------------------------------
# The terminal section is the loudest element of the report, and it answers TWO
# independent questions (CONTENT and CONTRACT). These renderers exist so a
# caveat written for one axis can never be pasted onto the other.


# --- competing-origin hypotheses ----------------------------------------
# Each hypothesis is `{origin, agent, basis, weight}`. The basis explains WHY
# this origin is live; it is keyed on a code so the UI (and a cross-run query)
# can group hypotheses by their kind instead of by a sentence.

HYPOTHESIS_REPORTED = "reported_origin"
HYPOTHESIS_LATER_PRODUCER = "later_producer"
HYPOTHESIS_UNRESOLVED = "unresolved"

_HYPOTHESIS_TEMPLATES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    HYPOTHESIS_REPORTED: lambda d: (
        "reported origin — where the score gap / content flag localised the "
        "fault"
    ),
    HYPOTHESIS_LATER_PRODUCER: lambda d: (
        "later origin (" + " + ".join(d["signals"]) + ") — the same "
        "content survived to this producer, so the loss may be at the "
        "render/export step, later than reported"
    ),
    HYPOTHESIS_UNRESOLVED: lambda d: (
        "unresolved — the evidence streams do not localise a single origin"
    ),
}


def render_hypothesis_basis(code: str, params: Mapping[str, Any] | None = None) -> str:
    tpl = _HYPOTHESIS_TEMPLATES.get(code)
    return tpl(params or {}) if tpl is not None else code


# --- stale-verdict cause -------------------------------------------------
# WHY a deterministic basis stopped reproducing, settled by the stored rule-set
# fingerprint (migration 0008). A code, because the three causes route to three
# DIFFERENT owners: nobody, the operator, the agent integration.

STALE_CAUSE_UNSTAMPED = "unstamped"
STALE_CAUSE_RULES_CHANGED = "rules_changed"
STALE_CAUSE_PAYLOAD_DIVERGED = "payload_diverged"

_STALE_CAUSE_TEMPLATES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    STALE_CAUSE_UNSTAMPED: lambda d: (
        "cause unknown — the tier1 verdict predates "
        "rule-set stamping (no fingerprint stored); "
        "cannot distinguish a rule change from "
        "artifact/payload divergence. Staleness is a "
        "property of the stored ANALYSIS, not a new "
        "fault of the agent's run"
    ),
    STALE_CAUSE_RULES_CHANGED: lambda d: (
        "the registered rule set CHANGED since tier1 "
        f"ran (fingerprint {d['stored']} -> "
        f"{d['current']}) — the old verdict was computed "
        "under different rules; not an artifact "
        "divergence. This is an ANALYSIS/rule-lifecycle "
        "matter on the operator side — the agent's run "
        "did not change and is not newly at fault"
    ),
    STALE_CAUSE_PAYLOAD_DIVERGED: lambda d: (
        "the rule set is UNCHANGED (fingerprint "
        f"{d['current']}) yet the payload no longer fails "
        "the check — the artifact/payload itself "
        "diverged between analysis passes "
        "(representation divergence); investigate the "
        "instrumentation/export path on the AGENT "
        "integration side"
    ),
}


def render_stale_cause(code: str, params: Mapping[str, Any] | None = None) -> str:
    tpl = _STALE_CAUSE_TEMPLATES.get(code)
    return tpl(params or {}) if tpl is not None else code


def render_breaker_reason(open_incidents: int, threshold: int, trigger: str) -> str:
    return (
        f"{open_incidents} open incidents (threshold {threshold}); "
        f"latest trigger {trigger}"
    )


def render_opaque_deliverable_reason(refs: list[str]) -> str:
    """The tier1 not-checkable reasoning for a deliverable whose artifact
    content never reached the payload."""
    return (
        "deliverable references a file artifact whose content was not "
        f"embedded ({', '.join(refs[:3])}) — cannot verify the goal"
    )


def render_opaque_artifact_note(refs: list[str]) -> str:
    """The scoring-side counterpart, appended to the judge note."""
    return "artifact content not in payload (unverifiable): " + ", ".join(refs[:3])


def render_uninspected_media_caveat(refs: list[str]) -> str:
    """The limit on a deliverable whose text IS present but which illustrates
    itself with images nobody opened.

    The verdict stands — it was reached on text we actually read — but it is
    stated as what it is. Saying "verified" flat would claim the pictures were
    checked too; withholding the verdict entirely (the old behaviour) threw away
    a reading of a complete markdown dossier because of a thumbnail.

    Worded for the payload, not for the deliverable: the same caveat rides on
    per-node judge notes, where "deliverable" would be a lie about which output
    was read."""
    return (
        f"verified on the payload's text only: {len(refs)} referenced "
        f"image(s) were not inspected ({', '.join(refs[:3])})"
    )


def render_superseded_reason(shipped: list[dict]) -> str:
    """Why the engine's near-miss framing was superseded by the escalation."""
    detail = "; ".join(
        f"{r['key']}: {r['from']!r}->{r['to']!r} ({r['basis']})" for r in shipped
    )
    return (
        "the near-miss framing was refuted: the "
        f"contract breach ({detail}) was VERIFIED in "
        "the shipped artifact — verdict escalated to "
        "shipped_with_latent_defect"
    )


def _conformance_detail(shipped: list[dict]) -> str:
    return "; ".join(
        f"{r['key']} {r['to']!r} shipped, {r['from']!r} required" for r in shipped
    )


def render_conformance(shipped: list[dict]) -> str:
    return f"nonconformant (verified): {_conformance_detail(shipped)}"


def render_shipped_caveat(shipped: list[dict], *, content: str) -> str:
    """The caveat for a VERIFIED shipped breach, keyed on the CONTENT axis.

    ``content`` is "stale" | "bad" | "ok" — the three mutually exclusive states
    of the other axis. Keying the template on it is what makes "ok in CONTENT
    only" over a bad content verdict unwritable.
    """
    detail = _conformance_detail(shipped)
    if content == "stale":
        # The CONTENT axis is discarded (not reproducible) — it is neither ok nor
        # bad. Only the contract axis stands; "ok in CONTENT only" here would
        # assert a content verdict we just threw away.
        return (
            f"content verdict is STALE (not reproducible — "
            f"see the cause above); the contract axis "
            f"stands on its own evidence: "
            f"contract-nonconformant deliverable VERIFIED "
            f"— {detail} (see contract_propagation)"
        )
    if content == "bad":
        return (
            f"TWO independent faults: content is bad (see "
            f"reasoning) AND the deliverable is "
            f"contract-nonconformant — {detail} (verified, "
            "see contract_propagation)"
        )
    return (
        f"ok in CONTENT only — contract-nonconformant "
        f"deliverable VERIFIED: {detail} (see "
        "contract_propagation)"
    )


def render_corrected_caveat() -> str:
    return (
        "ok — the mid-pipeline contract breach was "
        "corrected downstream (verified); the deliverable "
        "conforms to the carried contract"
    )
