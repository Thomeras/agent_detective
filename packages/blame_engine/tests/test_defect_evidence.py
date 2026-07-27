"""Defect evidence discipline (the report-15/16 review fixes).

Locks the four behaviours the corpus reviews demanded:

1. **Ref polarity + validator** — every defect carries ≥1 SUPPORTING finding;
   a defect built from exculpatory evidence alone is unconstructible, and
   counter-evidence is kept visible as ``refuting`` refs.
2. **Per-culprit channel/kind** — a contract-breach culprit yields a
   deterministic CONTRACT defect; deterministic findings (breach, section
   signal) are never orphaned while the defect ships as judged content.
3. **Form defect anchoring** — origination ≠ non-detection: a file-format
   breach that explains the shipped form localizes the form defect at the
   breaching node; the design gap stays a separate finding about detection.
4. **Requirement reconcile** — contract reference (docx) vs judge-read
   requirement ('jako PDF') share a fact_key and MUST produce a divergence;
   verified propagation (breach_propagated) is cited by the contract defect and
   drives the in-engine escalation, with the unverified-in-content caveat gone.
"""

import pytest

from blame_engine import (
    Defect,
    Finding,
    FindingRef,
    JudgePrompt,
    Localized,
    NodeScore,
    TerminalVerdict,
    RuleFingerprint,
    deserialize_defect,
    file_type_token,
    find_blame,
)
from blame_engine.assemble import finalize_schema2, validate_defects


def _score(run_id, value, *, contract=(), flags=(), signals=(), note="judged"):
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=False,
        unscored_reason=None,
        judge_note=note,
        flags=tuple(flags),
        contract_violations=tuple(contract),
        deterministic_signals=tuple(signals),
    )


_SECTION_SIGNAL = {
    "name": "missing_required_section",
    "severity": "fail",
    "section": "brand config block",
    "detail": "required section 'brand config block' not found",
    "basis": "substring 'sub_brand' not present in this node's own output",
}

# The report-15 terminal: content ok (1.0), form bad — 'jako PDF' requested,
# markdown shipped.
_TERMINAL_FORM_BAD = TerminalVerdict(
    bad=False,
    score=1.0,
    reasoning="comprehensive quarterly report, aligns with the initial request",
    checkable=True,
    form_bad=True,
    form_requirement="jako PDF",
    form_observed="markdown text",
)


def _report15(mk):
    """The corpus report-15 shape: think breaches the contract (docx->md),
    render trips a deterministic section check at judged score 1.0, terminal
    content ok, form bad. Two independent deterministic origins."""
    inp = mk(
        nodes=["start", "think", "act", "render"],
        edges=[("start", "think"), ("think", "act"), ("act", "render")],
        scores={
            "start": None,
            "think": _score(
                "think",
                0.5636,
                contract=[("file_type", "docx", "md")],
                flags=["missing_required_content"],
            ),
            "act": _score("act", 0.93),
            "render": _score("render", 1.0, signals=[_SECTION_SIGNAL]),
        },
        terminal_verdict=_TERMINAL_FORM_BAD,
    )
    return find_blame(inp)


def _typed(report):
    findings = report.evidence.findings
    defects = [deserialize_defect(d) for d in report.evidence.defects]
    return findings, defects


def _supporting_kinds(findings, defect):
    return {
        findings[r["ref"] if isinstance(r, dict) else r.ref]["kind"]
        for r in defect.finding_refs
        if (r.role if hasattr(r, "role") else r["role"]) == "supporting"
    }


# --- 2: per-culprit channel/kind ------------------------------------------


def test_contract_culprit_yields_deterministic_contract_defect(mk):
    report = _report15(mk)
    findings, defects = _typed(report)

    contract = [d for d in defects if d.kind == "contract"]
    assert len(contract) == 1
    d = contract[0]
    assert d.channel == "deterministic"
    assert isinstance(d.origin, Localized) and d.origin.run_id == "think"
    assert "contract_breach" in _supporting_kinds(findings, d)


def test_deterministic_section_finding_is_cited_not_orphaned(mk):
    report = _report15(mk)
    findings, defects = _typed(report)

    render_content = [
        d
        for d in defects
        if d.kind == "content"
        and isinstance(d.origin, Localized)
        and d.origin.run_id == "render"
    ]
    assert len(render_content) == 1
    d = render_content[0]
    # Localized by a deterministic check on its own output → the defect IS
    # deterministic and the check is its supporting evidence.
    assert d.channel == "deterministic"
    assert "deterministic_signal" in _supporting_kinds(findings, d)
    # The judged 1.0 score and the ok terminal are kept as REFUTING refs — the
    # tension stays visible, never rendered as "the evidence for this defect".
    refuting = {findings[r.ref]["kind"] for r in d.finding_refs if r.role == "refuting"}
    assert "content_score" in refuting
    assert "terminal_content" in refuting


def test_no_defect_cites_only_exculpatory_or_context_evidence(mk):
    _, defects = _typed(_report15(mk))
    for d in defects:
        assert any(r.role == "supporting" for r in d.finding_refs), d.kind


def test_every_certainty_one_finding_referenced_by_some_defect(mk):
    """The report-15 scandal: all three deterministic facts (breach, section
    signal, divergence) were orphans while the verdict rode the judge."""
    report = _report15(mk)
    findings, defects = _typed(report)
    referenced = {r.ref for d in defects for r in d.finding_refs}
    for i, f in enumerate(findings):
        if f["certainty"] == 1.0:
            assert i in referenced, f["kind"]


# --- 3: form anchoring ----------------------------------------------------


def test_form_defect_localizes_at_the_breaching_node(mk):
    """docx->md at think + 'markdown text' shipped = one causal chain: the form
    defect is Localized(think) via the deterministic breach, NOT a Design guess."""
    report = _report15(mk)
    findings, defects = _typed(report)

    form = [d for d in defects if d.kind == "form"]
    assert len(form) == 1
    d = form[0]
    assert isinstance(d.origin, Localized) and d.origin.run_id == "think"
    assert d.channel == "deterministic"
    kinds = _supporting_kinds(findings, d)
    assert "contract_breach" in kinds
    assert "terminal_form" in kinds
    # Non-detection stays a SEPARATE fact, referenced as context.
    ctx = {findings[r.ref]["kind"] for r in d.finding_refs if r.role == "context"}
    assert "detection_gap" in ctx


def test_form_defect_without_breach_anchor_stays_design(mk):
    """No file-format breach to anchor on → the origin falls back to Design and
    the detection gap is the supporting fact for that claim."""
    inp = mk(
        nodes=["start", "think", "render"],
        edges=[("start", "think"), ("think", "render")],
        scores={"start": None, "think": _score("think", 0.9), "render": _score("render", 0.95)},
        terminal_verdict=_TERMINAL_FORM_BAD,
    )
    findings, defects = _typed(find_blame(inp))
    form = [d for d in defects if d.kind == "form"]
    assert len(form) == 1
    assert type(form[0].origin).__name__ == "Design"
    assert "detection_gap" in _supporting_kinds(findings, form[0])


# --- 4: requirement reconcile + propagation -------------------------------


def test_requirement_divergence_fires_for_pdf_vs_docx(mk):
    """The judges were right all along: initial_input asks for PDF. The contract
    scaffold says docx. Same fact, two values, full provenance on both — the
    reconcile pass MUST surface it (requirement_provenance divergence), refs
    pointing at both sides."""
    report = _report15(mk)
    findings, _ = _typed(report)

    div = [f for f in findings if f["kind"] == "requirement_provenance"]
    assert len(div) == 1
    d = div[0]
    assert d["fact_key"] == "contract:file_type"
    assert d["data"]["values"] == ["docx", "pdf"]
    member_kinds = {findings[i]["kind"] for i in d["data"]["finding_refs"]}
    assert member_kinds == {"contract_breach", "terminal_form"}


def test_divergence_is_attached_as_context_to_the_defects_it_reconciles(mk):
    report = _report15(mk)
    findings, defects = _typed(report)
    div_idx = next(
        i for i, f in enumerate(findings) if f["kind"] == "requirement_provenance"
    )
    contract = next(d for d in defects if d.kind == "contract")
    form = next(d for d in defects if d.kind == "form")
    for d in (contract, form):
        assert any(r.ref == div_idx and r.role == "context" for r in d.finding_refs)


def _degraded_recovered_input(mk):
    return mk(
        nodes=["start", "think", "act", "render"],
        edges=[("start", "think"), ("think", "act"), ("act", "render")],
        scores={
            "start": None,
            "think": _score(
                "think", 0.15, contract=[("file_type", "docx", "md")],
                flags=["missing_required_content"],
            ),
            "act": _score("act", 0.93),
            "render": _score("render", 0.93),
        },
        terminal_verdict=TerminalVerdict(
            bad=False, score=1.0,
            reasoning="comprehensive overview, aligns with the request",
            checkable=True,
        ),
    )


_PROPAGATED = Finding(
    kind="breach_propagated",
    channel="deterministic",
    subject="terminal",
    data={
        "key": "file_type",
        "from": "docx",
        "to": "md",
        "basis": "artifact path 'report.md' ends '.md'",
        "deliverable_run_id": "render",
    },
    provenance=RuleFingerprint(
        rule="contract_propagation:file_type", detail="artifact path 'report.md' ends '.md'"
    ),
    certainty=1.0,
)


def test_breach_propagated_escalates_in_engine_and_is_cited(mk):
    """extra_findings carry the worker-verified propagation BEFORE derivation:
    the engine escalates in the single pass, the contract defect cites the
    breach_propagated finding as support, records the propagation path, and the
    'unverified in content' caveat is GONE (it may not stand under a verified
    shipped headline)."""
    report = find_blame(_degraded_recovered_input(mk), extra_findings=[_PROPAGATED])
    findings, defects = _typed(report)

    assert report.report_type == "shipped_with_latent_defect"
    contract = next(d for d in defects if d.kind == "contract")
    assert "breach_propagated" in _supporting_kinds(findings, contract)
    assert contract.propagation == ("render",)
    assert contract.unverified_in_channel is None


def test_without_propagation_finding_caveat_and_verdict_stay_honest(mk):
    report = find_blame(_degraded_recovered_input(mk))
    _, defects = _typed(report)
    assert report.report_type == "degraded_recovered"
    contract = next(d for d in defects if d.kind == "contract")
    assert contract.unverified_in_channel == "content"
    assert contract.propagation == ()


# --- 1: validator ---------------------------------------------------------


def test_validator_rejects_defect_without_supporting_finding():
    findings = [
        Finding(
            kind="content_score",
            channel="judged",
            subject="run:render",
            data={"score": 1.0},
            provenance=JudgePrompt(),
            certainty=0.7,
        )
    ]
    bad = Defect(
        kind="content",
        channel="judged",
        origin=Localized(run_id="render"),
        finding_refs=(FindingRef(0, "refuting"),),
    )
    with pytest.raises(ValueError, match="no supporting"):
        validate_defects(findings, [bad])


def test_validator_rejects_propagation_claim_without_propagation_finding():
    findings = [
        Finding(
            kind="contract_breach",
            channel="deterministic",
            subject="run:think",
            data={"key": "file_type", "from": "docx", "to": "md"},
            provenance=RuleFingerprint(rule="input_contract:file_type"),
            certainty=1.0,
        )
    ]
    bad = Defect(
        kind="contract",
        channel="deterministic",
        origin=Localized(run_id="think"),
        finding_refs=(FindingRef(0, "supporting"),),
        propagation=("render",),
    )
    with pytest.raises(ValueError, match="propagation"):
        validate_defects(findings, [bad])


def test_validator_rejects_out_of_range_ref():
    bad = Defect(
        kind="content",
        channel="judged",
        origin=Localized(run_id="x"),
        finding_refs=(FindingRef(3, "supporting"),),
    )
    with pytest.raises(ValueError, match="out of range"):
        validate_defects([], [bad])


# --- assessment_conflict: the verifier-lane confabulation, typed ----------


def _ns_flawed(run_id, value):
    return NodeScore(
        run_id=run_id,
        score=value,
        components={},
        input_flawed=True,
        unscored_reason=None,
        judge_note="the reviewed artifact lacks required evidence notes",
        flags=(),
        contract_violations=(),
    )


def _verifier_lane_input(mk, *, terminal):
    """producer chain + qa/eval verifier lane; both verifiers claim their
    reviewed input flawed (the byte-stable 'missing evidence notes'
    confabulation exemplar from the 20-run variance)."""
    return mk(
        nodes=["start", "think", "render", "qa", "eval"],
        edges=[("start", "think"), ("think", "render"), ("render", "qa"), ("qa", "eval")],
        scores={
            "start": None,
            "think": _score("think", 0.9),
            "render": _score("render", 0.95),
            "qa": _ns_flawed("qa", 0.6),
            "eval": _ns_flawed("eval", 0.6),
        },
        terminal_verdict=terminal,
    )


def test_verifier_lane_flawed_claim_vs_ok_terminal_is_assessment_conflict(mk):
    """Verifier lane says the work is flawed, the checkable terminal says ok —
    one fact, two judged values → the reconcile pass MUST surface an
    assessment_conflict citing all three sides (§11 row 3, live exemplar:
    the 'missing evidence notes' confabulation, 20/20 stable)."""
    ok = TerminalVerdict(bad=False, score=1.0, reasoning="deliverable is good", checkable=True)
    report = find_blame(_verifier_lane_input(mk, terminal=ok))
    findings = report.evidence.findings

    conflicts = [f for f in findings if f["kind"] == "assessment_conflict"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["fact_key"] == "assessment:deliverable"
    assert c["data"]["values"] == ["bad", "ok"]
    member_kinds = {findings[i]["kind"] for i in c["data"]["finding_refs"]}
    assert member_kinds == {"input_flawed", "terminal_content"}


def test_verifier_lane_flawed_claim_with_bad_terminal_agrees_no_conflict(mk):
    """When the terminal really is bad, the verifier's flawed-input claim AGREES
    with reality — no conflict is manufactured."""
    bad = TerminalVerdict(bad=True, score=0.1, reasoning="empty deliverable", checkable=True)
    report = find_blame(_verifier_lane_input(mk, terminal=bad))
    assert not any(
        f["kind"] == "assessment_conflict" for f in report.evidence.findings
    )


def test_mid_pipeline_verifier_flawed_claim_gets_no_assessment_key(mk):
    """A verifier feeding a PRODUCER reviews an intermediate artifact — a
    different fact than the terminal deliverable; its input_flawed claim must
    not reconcile against the terminal (a genuinely flawed intermediate that
    downstream repaired would otherwise fabricate a conflict)."""
    ok = TerminalVerdict(bad=False, score=1.0, reasoning="deliverable is good", checkable=True)
    inp = mk(
        nodes=["start", "think", "qa", "render"],
        edges=[("start", "think"), ("think", "qa"), ("qa", "render")],
        scores={
            "start": None,
            "think": _score("think", 0.9),
            "qa": _ns_flawed("qa", 0.6),   # reviews think's output, feeds render
            "render": _score("render", 0.95),
        },
        terminal_verdict=ok,
    )
    findings = find_blame(inp).evidence.findings
    assert not any(f["kind"] == "assessment_conflict" for f in findings)
    flawed = [f for f in findings if f["kind"] == "input_flawed"]
    assert flawed and all(f["fact_key"] is None for f in flawed)


# --- file-type token unit -------------------------------------------------


def test_file_type_token_normalizes_requirement_phrases():
    assert file_type_token("jako PDF") == "pdf"
    assert file_type_token("markdown text") == "md"
    assert file_type_token("as a .docx document") == "docx"
    assert file_type_token("a coherent report") is None
    assert file_type_token(None) is None
