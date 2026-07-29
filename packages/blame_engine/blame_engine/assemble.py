"""Assemble the schema-2 typed layers (Finding[] + Defect[]) (verdict refactor §2).

The cascade calls one emitter per localization outcome it reached; the emitter
builds the typed Defect[] with REFS THAT CARRY POLARITY: every ref says whether
the finding supports, refutes, or merely contextualizes the defect. Two
invariants are enforced at finalize (``validate_defects``):

- **No defect without a supporting finding** (§2.4 no-unsupported-sentence).
  A defect citing only exculpatory evidence — "content defect at render" whose
  refs are render=1.0 and terminal=ok — becomes unconstructible, not a UI
  surprise.
- **A defect that claims propagation must cite a propagation finding.** The
  shipped_with_latent_defect headline may never assert "verified … shipped"
  while the evidence list holds no verification.

The defect's ``channel`` is DERIVED from its supporting findings — a defect is
deterministic exactly when deterministic evidence asserts it. The builder can
no longer stamp "judged" over a contract breach with certainty 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .defect import (
    REASON_NO_CONTENT_CANDIDATE,
    REASON_NO_FORM_VERIFIER,
    REASON_ORCHESTRATION_LAYER,
    Defect,
    Design,
    External,
    FindingRef,
    Localized,
    Unlocalized,
)
from .derive import reconcile
from .finding import (
    PROV_PER_NODE_QUALITY_DELTA,
    PROV_PER_NODE_QUALITY_JUDGE,
    PROV_TERMINAL_JUDGE_CONTENT,
    PROV_INPUT_OUTPUT_DIFF,
    PROV_TERMINAL_JUDGE_FORM,
    PROV_VERIFIER_CHARTER_ROSTER,
    PROV_VERIFIER_JUDGE,
    Finding,
    HarnessState,
    JudgePrompt,
    RuleFingerprint,
    UserRequest,
    file_type_token,
    run_subject,
    serialize_finding,
)
from .defect import serialize_defect

# Structured scoring flags that assert a CONTENT defect (mirrors blame._CONTENT_FLAGS).
CONTENT_FLAGS = frozenset(
    {"missing_required_content", "ignored_instruction", "factual_error"}
)

# File-format contract keys (mirrors the worker's propagation-check set): a
# breach on one of these can explain a shipped-form miss, so the form defect
# anchors on it instead of a Design guess.
_FILE_FORMAT_KEYS = frozenset({"file_type", "filetype", "format", "target_format"})


@dataclass
class FindingIndex:
    """The built Finding[] plus lookups from run_id to the finding indices that
    let a Defect reference the findings that show it (the §2.4 no-unsupported-
    sentence invariant: a defect must hold a ref to a finding)."""

    findings: list[Finding]
    threshold: float
    contract_by_run: dict[str, list[int]]
    content_score_by_run: dict[str, int]
    content_flag_by_run: dict[str, list[int]]
    det_sig_by_run: dict[str, list[int]]
    drop_by_run: dict[str, int]
    input_flawed_by_run: dict[str, int]
    loop_by_run: dict[str, list[int]]
    verifier_by_run: dict[str, list[int]]
    terminal_content: int | None
    terminal_form: int | None
    detection_gap: int | None
    # worker-verified contract propagation (extra_findings), keyed by contract key
    propagated_by_key: dict[str, int]
    corrected_by_key: dict[str, int]


def build_findings(
    inp,
    graph_nodes,
    contract_breaches,
    deterministic_signals,
    verification_gaps,
    tv,
    anomalies,
    candidates=(),
    threshold: float = 0.5,
    assessment_lane=frozenset(),
    loop_runs=frozenset(),
) -> FindingIndex:
    findings: list[Finding] = []
    contract_by_run: dict[str, list[int]] = {}
    content_score_by_run: dict[str, int] = {}
    content_flag_by_run: dict[str, list[int]] = {}
    det_sig_by_run: dict[str, list[int]] = {}
    drop_by_run: dict[str, int] = {}
    input_flawed_by_run: dict[str, int] = {}
    loop_by_run: dict[str, list[int]] = {}
    verifier_by_run: dict[str, list[int]] = {}

    def add(f: Finding) -> int:
        findings.append(f)
        return len(findings) - 1

    # content_score + content_flag + input_flawed findings (judged), per scored node.
    for n in graph_nodes:
        ns = inp.scores.get(n)
        if ns is None:
            continue
        if ns.score is not None:
            content_score_by_run[n] = add(
                Finding(
                    kind="content_score",
                    channel="judged",
                    subject=run_subject(n),
                    # The judge's own words travel WITH the number. They were
                    # dropped here, so a reader looking at a finding saw
                    # "plan scored 0.56" and had no way to learn why — the
                    # reasoning existed, it just never left the NodeScore. It
                    # is the only thing on a judged finding a human can check.
                    data={
                        "score": ns.score,
                        "agent": inp.agent_names.get(n, n),
                        **({"reasoning": ns.judge_note} if ns.judge_note else {}),
                    },
                    provenance=JudgePrompt(detail=PROV_PER_NODE_QUALITY_JUDGE),
                    certainty=0.7,
                )
            )
        for flag in ns.flags:
            if flag in CONTENT_FLAGS:
                content_flag_by_run.setdefault(n, []).append(
                    add(
                        Finding(
                            kind="content_flag",
                            channel="judged",
                            subject=run_subject(n),
                            data={
                                "flag": flag,
                                "agent": inp.agent_names.get(n, n),
                                **({"reasoning": ns.judge_note} if ns.judge_note else {}),
                            },
                            provenance=JudgePrompt(detail=PROV_PER_NODE_QUALITY_JUDGE),
                            certainty=0.7,
                        )
                    )
                )
        if ns.input_flawed is True:
            # A terminal-review verifier's "input flawed" claim measures the
            # same fact as the terminal content verdict (the final work
            # product's quality per assessment) — shared fact_key so the
            # reconcile pass surfaces an assessment_conflict when the checkable
            # terminal says ok. The byte-stable confabulation pattern (a judge
            # inventing an unregistered requirement, 20/20 across the variance
            # run) thereby becomes a typed, queryable finding instead of a
            # night-run footnote.
            lane = n in assessment_lane
            input_flawed_by_run[n] = add(
                Finding(
                    kind="input_flawed",
                    channel="judged",
                    subject=run_subject(n),
                    data={
                        "agent": inp.agent_names.get(n, n),
                        **({"value": "bad"} if lane else {}),
                    },
                    provenance=JudgePrompt(detail=PROV_PER_NODE_QUALITY_JUDGE),
                    certainty=0.7,
                    fact_key="assessment:deliverable" if lane else None,
                )
            )

    # content_drop findings — the measured score delta that LOCALIZED a content
    # candidate. This is the fact a drop-based content defect cites as support
    # (its absolute score alone can sit above threshold while the drop is real).
    # Candidates inside a multi-member SCC are SKIPPED here: their build-time
    # drop is measured at the loop's EXIT, but blame drills to the worst
    # MEMBER — emitting the exit drop would orphan a fact on a node no defect
    # blames while the drilled defect lost its localization fact (the mesh
    # golden caught exactly this). The cascade emits the drilled member's REAL
    # drop via ``ensure_content_drop_finding`` once drilling has decided.
    for c in candidates:
        via = getattr(c, "via", "content")
        cumulative = list(getattr(c, "cumulative_path", None) or ())
        if via not in ("content", "both") and not cumulative:
            continue
        if c.run_id in drop_by_run or c.run_id in loop_runs:
            continue
        drop_by_run[c.run_id] = add(
            Finding(
                kind="content_drop",
                channel="judged",
                subject=run_subject(c.run_id),
                data={
                    "score": c.score,
                    "base": c.base,
                    "drop": c.drop,
                    "agent": inp.agent_names.get(c.run_id, c.run_id),
                    **({"cumulative_path": cumulative} if cumulative else {}),
                },
                provenance=JudgePrompt(detail=PROV_PER_NODE_QUALITY_DELTA),
                certainty=0.7,
            )
        )

    # contract_breach findings (deterministic). ``value`` is the contract's
    # REFERENCE value for the carried parameter — the reconcile pass compares it
    # against the judge-read requirement under the same fact_key, so a contract
    # scaffold that diverges from the user's ask (docx vs 'jako PDF') surfaces
    # as a divergence instead of sitting silently next to it. NOTE the certainty
    # semantics: 1.0 means "the rule fired reproducibly on the diff", NOT "the
    # contract is anchored in the user's request" — anchoring lives in
    # provenance and in the reconcile verdict.
    for b in contract_breaches:
        rid = b["run_id"]
        contract_by_run.setdefault(rid, []).append(
            add(
                Finding(
                    kind="contract_breach",
                    channel="deterministic",
                    subject=run_subject(rid),
                    data={
                        "key": b["key"],
                        "from": b["from"],
                        "to": b["to"],
                        "value": b["from"],
                        "agent": b["agent"],
                    },
                    provenance=RuleFingerprint(
                        rule=f"input_contract:{b['key']}",
                        detail=PROV_INPUT_OUTPUT_DIFF,
                    ),
                    certainty=1.0,
                    fact_key=f"contract:{b['key']}",
                )
            )
        )

    # deterministic_signal findings (deterministic), indexed per run so a defect
    # can cite them. A missing_required_section signal carries the section's
    # fact_key + value so it takes part in the §2.4 reconcile against the
    # worker's producer-side representation.
    for sig in deterministic_signals:
        rid = sig.get("run_id")
        section = sig.get("section")
        is_section_miss = sig.get("name") == "missing_required_section" and section
        i = add(
            Finding(
                kind="deterministic_signal",
                channel="deterministic",
                subject=run_subject(rid) if rid else "graph",
                data={
                    "name": sig.get("name"),
                    "severity": sig.get("severity"),
                    "detail": sig.get("detail"),
                    "agent": sig.get("agent"),
                    **({"section": section, "value": "absent"} if is_section_miss else {}),
                },
                provenance=RuleFingerprint(rule=str(sig.get("name") or "signal")),
                certainty=1.0,
                fact_key=f"required_section:{section}" if is_section_miss else None,
            )
        )
        if rid:
            det_sig_by_run.setdefault(rid, []).append(i)

    # terminal_content + terminal_form findings (judged).
    terminal_content = None
    terminal_form = None
    detection_gap = None
    if tv is not None:
        # A checkable, non-stale terminal verdict carries the assessment
        # fact_key so the verifier lane's input_flawed claim reconciles against
        # it (values agree on "bad" → no conflict; verifier-bad vs terminal-ok
        # → assessment_conflict). A not-checkable/stale verdict claims nothing.
        _tc_checkable = tv.checkable and not tv.stale
        terminal_content = add(
            Finding(
                kind="terminal_content",
                channel="judged",
                subject="terminal",
                data={
                    "bad": tv.bad,
                    "score": tv.score,
                    "reasoning": tv.reasoning,
                    "checkable": tv.checkable,
                    "stale": tv.stale,
                    **({"value": "bad" if tv.bad else "ok"} if _tc_checkable else {}),
                },
                provenance=JudgePrompt(detail=PROV_TERMINAL_JUDGE_CONTENT),
                certainty=0.8 if tv.checkable else 0.2,
                fact_key="assessment:deliverable" if _tc_checkable else None,
            )
        )
        if tv.form_bad or tv.form_requirement is not None:
            # The requirement quote is the provenance that lets a form miss be
            # reconciled against a deterministic contract reference (§2.4). When
            # the quote names a file type, the finding carries the SAME fact_key
            # as the contract_breach — the reconcile pass is what surfaces
            # requirement (pdf) vs contract scaffold (docx).
            prov = (
                UserRequest(quote=tv.form_requirement)
                if tv.form_requirement
                else JudgePrompt(detail=PROV_TERMINAL_JUDGE_FORM)
            )
            req_token = file_type_token(tv.form_requirement)
            terminal_form = add(
                Finding(
                    kind="terminal_form",
                    channel="judged",
                    subject="terminal",
                    data={
                        "bad": tv.form_bad,
                        "requirement": tv.form_requirement,
                        "observed": tv.form_observed,
                        "reasoning": tv.form_reasoning,
                        **({"value": req_token} if req_token else {}),
                    },
                    provenance=prov,
                    certainty=0.8,
                    fact_key="contract:file_type" if req_token else None,
                )
            )
        if tv.checkable and tv.form_bad:
            # Non-detection is its own fact, separate from where the fault
            # ORIGINATED: "no verifier owns form vision" explains why nobody
            # caught the miss, never where it came from.
            detection_gap = add(
                Finding(
                    kind="detection_gap",
                    channel="judged",
                    subject="graph",
                    data={
                        "dimension": "form",
                        # A reason CODE, rendered by the narrative — the finding
                        # is a fact, not a sentence (§2.4).
                        "reason": REASON_NO_FORM_VERIFIER,
                    },
                    provenance=HarnessState(detail=PROV_VERIFIER_CHARTER_ROSTER),
                    certainty=0.6,
                )
            )

    # loop_anomaly findings (deterministic limit breach).
    for a in anomalies:
        idx = add(
            Finding(
                kind="loop_anomaly",
                channel="deterministic",
                subject=run_subject(a.member_run_ids[0]),
                data={
                    "iterations": a.iterations,
                    "limit_kind": a.limit_kind,
                    "members": list(a.member_run_ids),
                    "agents": list(a.agent_names),
                },
                provenance=RuleFingerprint(rule=f"loop:{a.limit_kind}"),
                certainty=1.0,
            )
        )
        for m in a.member_run_ids:
            loop_by_run.setdefault(m, []).append(idx)

    # verifier_verdict findings (a verifier whose PASS/FAIL was wrong).
    for g in verification_gaps:
        rid = g["run_id"]
        verifier_by_run.setdefault(rid, []).append(
            add(
                Finding(
                    kind="verifier_verdict",
                    channel="judged",
                    subject=run_subject(rid),
                    data={"basis": g.get("basis"), "agent": g.get("agent_name")},
                    provenance=JudgePrompt(detail=PROV_VERIFIER_JUDGE),
                    certainty=0.6,
                )
            )
        )

    return FindingIndex(
        findings=findings,
        threshold=threshold,
        contract_by_run=contract_by_run,
        content_score_by_run=content_score_by_run,
        content_flag_by_run=content_flag_by_run,
        det_sig_by_run=det_sig_by_run,
        drop_by_run=drop_by_run,
        input_flawed_by_run=input_flawed_by_run,
        loop_by_run=loop_by_run,
        verifier_by_run=verifier_by_run,
        terminal_content=terminal_content,
        terminal_form=terminal_form,
        detection_gap=detection_gap,
        propagated_by_key={},
        corrected_by_key={},
    )


def ensure_content_drop_finding(
    idx: FindingIndex, inp, run, *, score, base, drop, loop_members=()
) -> None:
    """Emit (and index) the content_drop localization fact for a culprit whose
    candidate sat inside a multi-member SCC — the drilled member's REAL in-loop
    drop, or the exit's own drop when drilling found no scored member. No-op if
    the run already has a drop finding or there is no measured drop."""
    if run in idx.drop_by_run or drop is None:
        return
    data = {
        "score": score,
        "base": base,
        "drop": drop,
        "agent": inp.agent_names.get(run, run),
    }
    if loop_members:
        data["loop_members"] = list(loop_members)
    idx.drop_by_run[run] = len(idx.findings)
    idx.findings.append(
        Finding(
            kind="content_drop",
            channel="judged",
            subject=run_subject(run),
            data=data,
            provenance=JudgePrompt(detail=PROV_PER_NODE_QUALITY_DELTA),
            certainty=0.7,
        )
    )


def add_extra_findings(idx: FindingIndex, extra_findings) -> None:
    """Append caller-supplied typed Findings BEFORE defect emission (§F2.2), so
    emitters can reference them (breach_propagated is what lets the contract
    defect prove its own 'shipped' claim) and the reconcile pass sees them."""
    for f in extra_findings:
        idx.findings.append(f)
        i = len(idx.findings) - 1
        key = f.data.get("key")
        if f.kind == "breach_propagated" and key is not None:
            idx.propagated_by_key.setdefault(str(key), i)
        elif f.kind == "breach_corrected" and key is not None:
            idx.corrected_by_key.setdefault(str(key), i)


def add_verifier_findings(idx: FindingIndex, inp, verification_gaps) -> None:
    """Append verifier_verdict findings AFTER the localization pass (they depend on
    verification_gaps, which depend on the culprits the cascade selected)."""

    def add(f: Finding) -> int:
        idx.findings.append(f)
        return len(idx.findings) - 1

    for g in verification_gaps:
        rid = g["run_id"]
        idx.verifier_by_run.setdefault(rid, []).append(
            add(
                Finding(
                    kind="verifier_verdict",
                    channel="judged",
                    subject=run_subject(rid),
                    data={"basis": g.get("basis"), "agent": g.get("agent_name")},
                    provenance=JudgePrompt(detail=PROV_VERIFIER_JUDGE),
                    certainty=0.6,
                )
            )
        )


# --- ref classification ---------------------------------------------------


def _ordered(refs) -> tuple[FindingRef, ...]:
    """Dedupe by index (first classification wins) and order by index."""
    seen: dict[int, FindingRef] = {}
    for r in refs:
        if r.ref not in seen:
            seen[r.ref] = r
    return tuple(sorted(seen.values(), key=lambda r: r.ref))


def _channel_from(idx: FindingIndex, refs) -> str:
    """A defect's channel is the channel of its supporting evidence: it is
    deterministic exactly when a deterministic finding asserts it."""
    return (
        "deterministic"
        if any(
            r.role == "supporting" and idx.findings[r.ref].channel == "deterministic"
            for r in refs
        )
        else "judged"
    )


def _terminal_content_ref(idx: FindingIndex, *, recovered: bool) -> list[FindingRef]:
    if idx.terminal_content is None:
        return []
    d = idx.findings[idx.terminal_content].data
    if d.get("stale") or not d.get("checkable"):
        role = "context"
    elif d.get("bad"):
        role = "supporting"
    else:
        # For a RECOVERED defect an ok terminal is part of the story (successors
        # recovered), not a refutation; for a live defect it is counter-evidence.
        role = "context" if recovered else "refuting"
    return [FindingRef(idx.terminal_content, role)]


def _content_refs(idx: FindingIndex, run: str, *, recovered: bool = False):
    """Classify every content-channel finding at ``run`` relative to the claim
    'this node's content is defective': the drop that localized it supports; a
    score below threshold supports, above refutes (a 0.56 over a 0.50 threshold
    is NOT degraded — only a flag would then hold the claim, visibly); flags and
    fail-severity deterministic signals support."""
    refs: list[FindingRef] = []
    drop_i = idx.drop_by_run.get(run)
    if drop_i is not None:
        refs.append(FindingRef(drop_i, "supporting"))
    score_i = idx.content_score_by_run.get(run)
    if score_i is not None:
        score = idx.findings[score_i].data.get("score")
        if drop_i is not None:
            role = "context"  # the drop carries the claim; the raw score alone does not
        elif score is not None and score < idx.threshold:
            role = "supporting"
        else:
            role = "refuting"
        refs.append(FindingRef(score_i, role))
    refs.extend(FindingRef(i, "supporting") for i in idx.content_flag_by_run.get(run, []))
    for i in idx.det_sig_by_run.get(run, []):
        sev = idx.findings[i].data.get("severity")
        refs.append(FindingRef(i, "supporting" if sev == "fail" else "context"))
    refs.extend(_terminal_content_ref(idx, recovered=recovered))
    return refs


def _canon_token(value) -> str | None:
    return file_type_token(str(value)) if value is not None else None


def _contract_refs_typed(idx: FindingIndex, run: str):
    """Refs for a contract defect at ``run``: the breaches support it; a
    worker-verified propagation supports its 'shipped' claim; a correction is
    context (the breach happened, its fate is known); a terminal_form judged
    observation matching the rewritten value is cross-channel context (the
    deterministic breach at the node + the judged observation of the shipped
    form make one chain)."""
    breach_idxs = idx.contract_by_run.get(run, [])
    refs: list[FindingRef] = [FindingRef(i, "supporting") for i in breach_idxs]
    propagation: list[str] = []
    verified = False
    for i in breach_idxs:
        key = str(idx.findings[i].data.get("key"))
        pi = idx.propagated_by_key.get(key)
        if pi is not None:
            verified = True
            refs.append(FindingRef(pi, "supporting"))
            drid = idx.findings[pi].data.get("deliverable_run_id")
            if drid:
                propagation.append(str(drid))
        ci = idx.corrected_by_key.get(key)
        if ci is not None:
            verified = True
            refs.append(FindingRef(ci, "context"))
    if idx.terminal_form is not None:
        tf = idx.findings[idx.terminal_form].data
        obs_tok = _canon_token(tf.get("observed"))
        if obs_tok and any(
            idx.findings[i].data.get("key") in _FILE_FORMAT_KEYS
            and _canon_token(idx.findings[i].data.get("to")) == obs_tok
            for i in breach_idxs
        ):
            refs.append(FindingRef(idx.terminal_form, "context"))
    return refs, propagation, verified


def _breakdown_attr(attribution_breakdown, defect: str, default):
    return next(
        (b["attribution"] for b in attribution_breakdown if b["defect"] == defect),
        default,
    )


# --- Localizer output: emit Defect[] from what was LOCALIZED (§2.2) ------
#
# Each function is the typed output of ONE localization outcome the cascade
# reached. It never receives a report_type string: report_type is DERIVED from
# these defects afterwards (``derive.derive_report_type``), so a verdict can
# never disagree with the evidence it rests on (§11 row 4 unrepresentable).


def _contract_defect(
    idx,
    run,
    *,
    observation_confidence,
    attribution_confidence,
    unverified_marker: bool,
):
    refs, propagation, verified = _contract_refs_typed(idx, run)
    return Defect(
        kind="contract",
        channel=_channel_from(idx, refs),
        origin=Localized(run_id=run),
        finding_refs=_ordered(refs),
        observation_confidence=observation_confidence,
        attribution_confidence=attribution_confidence,
        propagation=tuple(propagation),
        # "unverified in content" may only stand while the breach's fate in the
        # shipped artifact is actually unknown — never under a verified
        # propagation (the headline and the caveat would contradict).
        unverified_in_channel="content" if (unverified_marker and not verified) else None,
    )


def _content_defect(
    idx,
    run,
    *,
    via,
    base_assumed,
    recovered,
    observation_confidence,
    attribution_confidence,
):
    refs = _ordered(_content_refs(idx, run, recovered=recovered))
    return Defect(
        kind="content",
        channel=_channel_from(idx, refs),
        origin=Localized(run_id=run),
        finding_refs=refs,
        observation_confidence=observation_confidence,
        attribution_confidence=attribution_confidence,
        base_assumed=base_assumed,
        observability_boundary=base_assumed,
        recovered=recovered,
    )


def emit_external(idx, culprit, *, observation_confidence, attribution_confidence):
    """The fault entered from outside the observed graph (a flawed source). The
    source's own input_flawed admission is the supporting fact."""
    refs: list[FindingRef] = []
    if culprit:
        ii = idx.input_flawed_by_run.get(culprit)
        if ii is not None:
            refs.append(FindingRef(ii, "supporting"))
        refs.extend(_content_refs(idx, culprit))
    return [
        Defect(
            kind="content",
            channel=_channel_from(idx, refs),
            origin=External(run_id=culprit),
            finding_refs=_ordered(refs),
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
    ]


def emit_loop(idx, culprit, *, observation_confidence, attribution_confidence):
    """A localized deterministic loop-limit breach."""
    refs = _ordered(FindingRef(i, "supporting") for i in idx.loop_by_run.get(culprit, []))
    return [
        Defect(
            kind="loop",
            channel=_channel_from(idx, refs),
            origin=Localized(run_id=culprit),
            finding_refs=refs,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
    ]


def emit_multi(
    idx,
    culprits,
    *,
    via_by_run,
    base_assumed_by_run,
    terminal_bad,
    observation_confidence,
    attribution_confidence,
):
    """Two or more independent localized origins. Each culprit gets the defect
    its OWN localization channel shows — a contract-breach culprit yields a
    deterministic contract defect, not a hardcoded judged content defect."""
    defects: list[Defect] = []
    for c in culprits:
        has_contract = bool(idx.contract_by_run.get(c))
        via = via_by_run.get(c, "content")
        if has_contract:
            defects.append(
                _contract_defect(
                    idx,
                    c,
                    observation_confidence=observation_confidence,
                    attribution_confidence=attribution_confidence,
                    unverified_marker=not terminal_bad,
                )
            )
        if terminal_bad or not has_contract or via in ("content", "both"):
            defects.append(
                _content_defect(
                    idx,
                    c,
                    via=via,
                    base_assumed=base_assumed_by_run.get(c, False),
                    recovered=False,
                    observation_confidence=observation_confidence,
                    attribution_confidence=attribution_confidence,
                )
            )
    return defects


def emit_verification(idx, culprits, *, observation_confidence, attribution_confidence):
    """Verifier(s) whose PASS/FAIL was wrong, nothing else localized."""
    return [
        Defect(
            kind="verification",
            channel="judged",
            origin=Localized(run_id=c),
            finding_refs=_ordered(
                FindingRef(i, "supporting") for i in idx.verifier_by_run.get(c, [])
            ),
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        for c in culprits
    ]


def emit_composition(idx, *, observation_confidence, attribution_confidence):
    """Content observed bad at the terminal, unlocalizable — orchestration layer.
    The content defect is ``Unlocalized`` by construction (no candidate)."""
    refs = _ordered(_terminal_content_ref(idx, recovered=False))
    return [
        Defect(
            kind="content",
            channel="judged",
            origin=Unlocalized(reason=REASON_ORCHESTRATION_LAYER),
            finding_refs=refs,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
    ]


def emit_terminal_unlocalized(idx, culprit, *, observation_confidence):
    """Contract fault IS localized; the content defect observed at the terminal is
    ``Unlocalized`` by construction (no content candidate exists)."""
    from .confidence import DETERMINISTIC_ATTRIBUTION

    defects: list[Defect] = []
    if culprit and idx.contract_by_run.get(culprit):
        defects.append(
            _contract_defect(
                idx,
                culprit,
                observation_confidence=observation_confidence,
                attribution_confidence=DETERMINISTIC_ATTRIBUTION,
                unverified_marker=True,
            )
        )
    defects.append(
        Defect(
            kind="content",
            channel="judged",
            origin=Unlocalized(reason=REASON_NO_CONTENT_CANDIDATE),
            finding_refs=_ordered(_terminal_content_ref(idx, recovered=False)),
            observation_confidence=observation_confidence,
            attribution_confidence=None,
            observability_boundary=True,
        )
    )
    return defects


def emit_cut_point(
    idx,
    culprit,
    *,
    via,
    base_assumed,
    terminal_bad,
    attribution_breakdown,
    observation_confidence,
    attribution_confidence,
):
    """A localized ACTIVE origin: contract defect (if a breach exists here) and/or
    a localized content defect (when the terminal is bad, or there is no contract
    breach carrying the verdict)."""
    from .confidence import DETERMINISTIC_ATTRIBUTION

    defects: list[Defect] = []
    has_contract = bool(culprit and idx.contract_by_run.get(culprit))
    contract_attr = _breakdown_attr(
        attribution_breakdown, "contract_violation", DETERMINISTIC_ATTRIBUTION
    )
    content_attr = _breakdown_attr(
        attribution_breakdown, "content_degradation", attribution_confidence
    )
    # A localized content defect ships whenever the content channel actually
    # localized a drop here (via content/both), when there is no contract carrying
    # the verdict (a pure content cut_point), or when the terminal is bad.
    emits_content = terminal_bad or not has_contract or via in ("content", "both")
    if has_contract:
        defects.append(
            _contract_defect(
                idx,
                culprit,
                observation_confidence=observation_confidence,
                attribution_confidence=contract_attr,
                # A contract breach standing ALONE (deterministic channel only, no
                # content defect beside it and no bad terminal) has had nothing
                # said about it in the content channel — that is the state the
                # caveat exists to name. It used to be hardcoded False, so a
                # deterministic-only cut_point shipped with no caveat at all,
                # reading as though content had been checked and cleared.
                unverified_marker=not emits_content,
            )
        )
    if emits_content:
        defects.append(
            _content_defect(
                idx,
                culprit,
                via=via,
                base_assumed=base_assumed,
                recovered=False,
                observation_confidence=observation_confidence,
                attribution_confidence=content_attr,
            )
        )
    return defects


def emit_degraded_recovered(
    idx,
    culprit,
    *,
    via,
    base_assumed,
    attribution_breakdown,
    observation_confidence,
    attribution_confidence,
):
    """A near-miss the pipeline compensated for. A contract breach here leaves the
    shipped artifact unverified in content (unless the worker VERIFIED its fate);
    a mild content drop (no breach) is a RECOVERED content defect that keeps its
    origin but never derives a cut_point."""
    from .confidence import DETERMINISTIC_ATTRIBUTION

    defects: list[Defect] = []
    has_contract = bool(culprit and idx.contract_by_run.get(culprit))
    contract_attr = _breakdown_attr(
        attribution_breakdown, "contract_violation", DETERMINISTIC_ATTRIBUTION
    )
    content_attr = _breakdown_attr(
        attribution_breakdown, "content_degradation", attribution_confidence
    )
    if has_contract:
        defects.append(
            _contract_defect(
                idx,
                culprit,
                observation_confidence=observation_confidence,
                attribution_confidence=contract_attr,
                unverified_marker=True,
            )
        )
    elif culprit:
        defects.append(
            _content_defect(
                idx,
                culprit,
                via=via,
                base_assumed=base_assumed,
                recovered=True,
                observation_confidence=observation_confidence,
                attribution_confidence=content_attr,
            )
        )
    return defects


def emit_form(idx, tv):
    """Form defect. Origination and non-detection are DIFFERENT attributions:
    when a deterministic file-format contract breach explains the shipped form,
    the defect is Localized at that node (the causal chain), and the design gap
    ("no verifier owns form vision") stays a separate finding about why nobody
    caught it. Only without such an anchor does the origin fall back to Design.
    """
    if not (tv is not None and tv.checkable and tv.form_bad):
        return []
    refs: list[FindingRef] = []
    if idx.terminal_form is not None:
        refs.append(FindingRef(idx.terminal_form, "supporting"))
    anchor_run = None
    tf = idx.findings[idx.terminal_form].data if idx.terminal_form is not None else {}
    obs_tok = _canon_token(tf.get("observed"))
    if obs_tok:
        for run, idxs in idx.contract_by_run.items():
            for i in idxs:
                f = idx.findings[i]
                if (
                    f.data.get("key") in _FILE_FORMAT_KEYS
                    and _canon_token(f.data.get("to")) == obs_tok
                ):
                    anchor_run = run
                    refs.append(FindingRef(i, "supporting"))
                    pi = idx.propagated_by_key.get(str(f.data.get("key")))
                    if pi is not None:
                        refs.append(FindingRef(pi, "supporting"))
                    break
            if anchor_run:
                break
    if idx.detection_gap is not None:
        # For a Design origin the gap IS the origin claim; for a localized
        # origin it is context about non-detection.
        refs.append(
            FindingRef(idx.detection_gap, "supporting" if anchor_run is None else "context")
        )
    origin = (
        Localized(run_id=anchor_run)
        if anchor_run is not None
        else Design(reason=REASON_NO_FORM_VERIFIER)
    )
    return [
        Defect(
            kind="form",
            channel=_channel_from(idx, refs),
            origin=origin,
            finding_refs=_ordered(refs),
            observation_confidence=None,
            attribution_confidence=None,
        )
    ]


# --- validation + finalize ------------------------------------------------


def validate_defects(findings, defects) -> None:
    """§2.4 invariants, enforced loudly:

    - every ref is in range;
    - every defect holds ≥1 SUPPORTING finding (no defect built from
      exculpatory/context evidence alone);
    - a defect that claims propagation cites a breach_propagated finding.
    """
    n = len(findings)
    for d in defects:
        for r in d.finding_refs:
            if not (0 <= r.ref < n):
                raise ValueError(
                    f"defect {d.kind!r} references finding {r.ref} out of range 0..{n - 1}"
                )
        if not any(r.role == "supporting" for r in d.finding_refs):
            raise ValueError(
                f"defect {d.kind!r} ({type(d.origin).__name__}) has no supporting "
                "finding — §2.4 no-unsupported-sentence"
            )
        if d.propagation and not any(
            r.role == "supporting" and findings[r.ref].kind == "breach_propagated"
            for r in d.finding_refs
        ):
            raise ValueError(
                f"defect {d.kind!r} claims propagation {d.propagation} without a "
                "supporting breach_propagated finding"
            )


def finalize_schema2(
    idx: FindingIndex, defects: list[Defect], extra_findings=None
) -> tuple[list[dict], list[dict]]:
    """Run the mandatory reconcile pass, attach each divergence as context to the
    defects whose evidence it reconciles, validate the §2.4 invariants, and
    serialize the schema-2 (findings, defects) payload.

    ``extra_findings`` is a legacy seam (callers should pass extras to
    ``find_blame``, which indexes them BEFORE defect emission via
    ``add_extra_findings``); still honored here for direct users."""
    if extra_findings:
        idx.findings.extend(extra_findings)
    # Mandatory reconcile pass BEFORE the payload is finalized (§2.4).
    base = len(idx.findings)
    divergences = reconcile(idx.findings)
    idx.findings.extend(divergences)
    out: list[Defect] = []
    for d in defects:
        have = {r.ref for r in d.finding_refs}
        extra = tuple(
            FindingRef(base + j, "context")
            for j, div in enumerate(divergences)
            if set(div.data.get("finding_refs") or ()) & have
        )
        out.append(replace(d, finding_refs=d.finding_refs + extra) if extra else d)
    validate_defects(idx.findings, out)
    return (
        [serialize_finding(f) for f in idx.findings],
        [serialize_defect(d) for d in out],
    )
