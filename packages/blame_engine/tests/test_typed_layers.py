"""Unit tests for the schema-2 typed layers themselves (§2.4 mechanisms).

Covers the invariants that make whole error classes unrepresentable:
- reconcile: no report may carry two unreconciled values of one fact_key (§11 #1-3);
- narrative: templates interpolate ONLY fields of what they render, and reserve
  "ground truth" for deterministic findings (§11 #5, #11);
- serialize/deserialize round-trip for Finding and Defect (sum types survive).
"""

from blame_engine import (
    Defect,
    Design,
    External,
    Finding,
    FindingRef,
    HarnessState,
    JudgePrompt,
    Localized,
    RuleFingerprint,
    Unlocalized,
    UserRequest,
    deserialize_defect,
    reconcile,
    render_defect,
    render_finding,
    serialize_defect,
    serialize_finding,
)
from blame_engine.finding import _serialize_provenance


# --- reconcile (§2.4 fact identity + mandatory reconcile) ---------------


def _finding(kind, channel, subject, value, fact_key):
    return Finding(
        kind=kind,
        channel=channel,
        subject=subject,
        data={"value": value},
        provenance=RuleFingerprint(rule="x"),
        certainty=1.0,
        fact_key=fact_key,
    )


def test_reconcile_emits_divergence_on_disagreeing_fact_key():
    findings = [
        _finding("contract_breach", "deterministic", "run:think", "docx", "contract:file_type"),
        _finding("terminal_form", "judged", "terminal", "md", "contract:file_type"),
    ]
    out = reconcile(findings)
    assert len(out) == 1
    div = out[0]
    assert div.kind == "requirement_provenance"
    assert div.data["fact_key"] == "contract:file_type"
    assert set(div.data["values"]) == {"docx", "md"}
    assert div.data["reported_by"] == ["run:think", "terminal"]


def test_reconcile_silent_when_values_agree():
    findings = [
        _finding("a", "deterministic", "run:x", "md", "contract:file_type"),
        _finding("b", "deterministic", "run:y", "md", "contract:file_type"),
    ]
    assert reconcile(findings) == []


def test_reconcile_ignores_findings_without_a_fact_key_or_value():
    findings = [
        Finding("content_score", "judged", "run:x", {"score": 0.2},
                JudgePrompt(), 0.7, fact_key=None),
        Finding("content_score", "judged", "run:y", {"score": 0.9},
                JudgePrompt(), 0.7, fact_key=None),
    ]
    assert reconcile(findings) == []


def test_reconcile_property_no_unreconciled_pair_survives():
    """Property: after reconcile, every fact_key with >1 distinct value has a
    divergence finding covering it — a report can never print two versions of one
    fact without a reconcile verdict between them."""
    findings = [
        _finding("a", "deterministic", "run:1", 1, "required_section:budget"),
        _finding("b", "judged", "terminal", 2, "required_section:budget"),
        _finding("c", "deterministic", "run:2", "same", "file_type_requirement:x"),
        _finding("d", "judged", "terminal", "same", "file_type_requirement:x"),
    ]
    divergences = reconcile(findings)
    all_findings = findings + divergences
    by_key: dict[str, set[str]] = {}
    for f in all_findings:
        if f.fact_key and "value" in f.data:
            by_key.setdefault(f.fact_key, set()).add(str(f.data["value"]))
    for key, values in by_key.items():
        if len(values) > 1:
            assert any(
                d.data.get("fact_key") == key for d in divergences
            ), f"unreconciled fact_key {key} survived"


# --- narrative templates (§2.4 no unsupported sentence / taxonomy) ------


def test_render_finding_channel_words_carry_no_epistemic_title():
    """'ground truth' is BANNED outright (taxonomy revision): a deterministic
    finding renders as 'deterministic' (the rule fired reproducibly) — its
    reference value can itself diverge from the user's requirement, so no
    channel may claim epistemic finality."""
    det = Finding("contract_breach", "deterministic", "run:think",
                  {"key": "file_type", "from": "docx", "to": "md"},
                  RuleFingerprint(rule="input_contract:file_type"), 1.0,
                  fact_key="contract:file_type")
    judged = Finding("content_score", "judged", "run:think", {"score": 0.2},
                     JudgePrompt(), 0.7)
    assert "ground truth" not in render_finding(det)
    assert "(deterministic)" in render_finding(det)
    assert "assessment" in render_finding(judged)
    assert "ground truth" not in render_finding(judged)


def test_render_defect_interpolates_only_its_own_fields():
    d = Defect(
        kind="content",
        channel="judged",
        origin=Unlocalized(reason="no content origin exists"),
        base_assumed=True,
        observability_boundary=True,
        unverified_in_channel="contract",
    )
    text = render_defect(d)
    assert "content defect" in text
    assert "not localized" in text
    assert "no content origin exists" in text
    # Caveat FIELDS render as chips (never truncated away mid-sentence).
    assert "baseline assumed" in text
    assert "observability boundary" in text
    assert "unverified in contract" in text


def test_render_defect_origin_phrases():
    assert "localized at think" in render_defect(
        Defect("contract", "deterministic", Localized("think"))
    )
    assert "outside the graph" in render_defect(
        Defect("content", "judged", External(run_id="ingest"))
    )
    assert "design-level gap" in render_defect(
        Defect("form", "judged", Design(reason="no verifier owns form"))
    )


# --- serialize / deserialize round-trip (sum types survive) -------------


def test_provenance_serialization_keeps_the_kind():
    for prov, tag in [
        (UserRequest(quote="jako PDF"), "UserRequest"),
        (HarnessState(detail="scaffold"), "HarnessState"),
        (RuleFingerprint(rule="r"), "RuleFingerprint"),
        (JudgePrompt(detail="j"), "JudgePrompt"),
    ]:
        assert _serialize_provenance(prov)["kind"] == tag


def test_finding_serializes_to_json_friendly_dict():
    f = Finding("contract_breach", "deterministic", "run:x",
                {"key": "file_type", "from": "docx", "to": "md"},
                UserRequest(quote="as docx"), 1.0, fact_key="contract:file_type")
    s = serialize_finding(f)
    assert s["kind"] == "contract_breach"
    # The recorded fields, plus the DERIVED label (a render artifact carried
    # alongside so a UI needs no copy of the code→phrase table; never read back).
    assert s["provenance"] == {
        "kind": "UserRequest",
        "quote": "as docx",
        "source": "initial_input",
        "label": "user request (initial_input): 'as docx'",
    }
    assert s["fact_key"] == "contract:file_type"


def test_defect_round_trips_through_serialize():
    for origin in [Localized("n"), Unlocalized("r"), External(run_id="s"), Design("d")]:
        d = Defect(
            kind="content",
            channel="judged",
            origin=origin,
            finding_refs=(FindingRef(1, "supporting"), FindingRef(2, "refuting")),
            observation_confidence=0.95,
            attribution_confidence=0.6,
            base_assumed=True,
            unverified_in_channel="contract",
        )
        back = deserialize_defect(serialize_defect(d))
        assert back == d
