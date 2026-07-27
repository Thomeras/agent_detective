"""Corpus cassettes — recorded live evidence, replayed hermetically.

A cassette IS a recorded ``Finding[]``/``Defect[]`` from a live judge pass
(night_run.md corpus cells, 2026-07-24): A/B/C = the injected md-rewrite runs
(reports #17/#18/#19, post-defect-evidence shapes), #20 = the surviving
proposal trace (pre-split tier1 — no form dimension). Each is a labelled
ground-truth cell (`label: bad`, culprit think).

What the replay locks WITHOUT any judge in the path:

- the projection round-trips: ``derive_report_type(defects)`` over the recorded
  typed defects reproduces the recorded report_type;
- the §2.4 invariants hold on REAL evidence, not just synthetic fixtures:
  every defect has a supporting finding, propagation claims cite
  breach_propagated, refs are in range (``validate_defects``);
- every certainty-1.0 finding is referenced by some defect (the report-15
  orphan bug, asserted over the live corpus);
- serialization round-trips (findings and defects survive
  deserialize → serialize unchanged).

The cassettes are corpus artifacts: NEVER regenerate them to make a test pass —
a mismatch means the engine drifted from behaviour the corpus validated.
"""

import json
from pathlib import Path

import pytest

from blame_engine import (
    deserialize_defect,
    deserialize_finding,
    derive_report_type,
    provenance_label,
    serialize_defect,
    serialize_finding,
)
from blame_engine.assemble import validate_defects

_CORPUS = sorted((Path(__file__).parent / "fixtures" / "corpus").glob("report_*.json"))


def _load(path):
    payload = json.loads(path.read_text())
    findings = [deserialize_finding(f) for f in payload["findings"]]
    defects = [deserialize_defect(d) for d in payload["defects"]]
    return payload, findings, defects


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_cassette_report_type_round_trips(path):
    payload, _findings, defects = _load(path)
    # The stored type may be the ESCALATED one; derive gives the pre-escalation
    # projection. Escalation is keyed on a breach_propagated finding being
    # present — reproduce the same two-step here.
    derived = derive_report_type(defects)
    if payload["report_type"] == "shipped_with_latent_defect":
        assert derived == "degraded_recovered"
        assert any(f.kind == "breach_propagated" for f in _load(path)[1])
    else:
        assert derived == payload["report_type"]


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_cassette_satisfies_defect_evidence_invariants(path):
    _payload, findings, defects = _load(path)
    validate_defects(findings, defects)  # raises on violation


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_cassette_has_no_certainty_one_orphans(path):
    _payload, findings, defects = _load(path)
    referenced = {r.ref for d in defects for r in d.finding_refs}
    for i, f in enumerate(findings):
        if f.certainty == 1.0:
            assert i in referenced, f.kind


# Keys serialization DERIVES rather than records. A derived key may appear in a
# re-serialized payload without contradicting the cassette (it is a render
# artifact recomputed from the recorded fields, never read back — see
# ``deserialize_provenance``). Everything else must survive byte-identically:
# the cassettes are corpus artifacts and a changed RECORDED value means the
# engine drifted from behaviour the corpus validated.
_DERIVED_KEYS = {"label"}


def _recorded_only(value):
    if isinstance(value, dict):
        return {
            k: _recorded_only(v) for k, v in value.items() if k not in _DERIVED_KEYS
        }
    if isinstance(value, list):
        return [_recorded_only(v) for v in value]
    return value


def _as_recorded(roundtripped: dict, recorded: dict) -> dict:
    """The round-tripped payload projected onto the keys the cassette RECORDED.

    A cassette written before a schema field existed cannot contain that field,
    and a NEW field appearing in the round-trip is schema evolution, not the
    drift this test guards (see ``quality_unmeasured``, added when the
    deterministic-only verdict stopped deriving as "recovered"). Every recorded
    key is still compared value-for-value, and the no-drop assertion below still
    fails if one disappears — so the guard is unchanged in the direction that
    matters: a recorded value may never change.
    """
    return {k: v for k, v in roundtripped.items() if k in recorded}


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_cassette_serialization_round_trips(path):
    """Every RECORDED field survives deserialize → serialize unchanged.

    Nothing the cassette recorded may be lost, altered or reordered; the check
    is deliberately blind to keys the serializer derives (§2.4 render artifacts,
    pinned by the next test) and to keys the schema gained after the cassette was
    recorded (``_as_recorded``).
    """
    payload, findings, defects = _load(path)
    assert _recorded_only([serialize_finding(f) for f in findings]) == _recorded_only(
        payload["findings"]
    )
    rt_defects = [serialize_defect(d) for d in defects]
    assert len(rt_defects) == len(payload["defects"])
    assert [
        _recorded_only(_as_recorded(rt, rec))
        for rt, rec in zip(rt_defects, payload["defects"])
    ] == _recorded_only(payload["defects"])
    # Nothing recorded may be DROPPED either — the blindness above must not hide
    # a missing key, only an added derived/newer one.
    for recorded, roundtripped in zip(
        payload["findings"], [serialize_finding(f) for f in findings]
    ):
        assert set(recorded) - _DERIVED_KEYS <= set(roundtripped)
    for recorded, roundtripped in zip(payload["defects"], rt_defects):
        assert set(recorded) - _DERIVED_KEYS <= set(roundtripped)


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_cassette_provenance_labels_are_derived_not_recorded(path):
    """The provenance label is the rendered form of the recorded codes. Pinning
    it here is what lets the round-trip above ignore it: the label cannot drift
    silently, it just is not part of the recorded fact."""
    _payload, findings, _defects = _load(path)
    for f in findings:
        s = serialize_finding(f)
        assert s["provenance"]["label"] == provenance_label(f.provenance)


@pytest.mark.parametrize("path", _CORPUS, ids=lambda p: p.stem)
def test_cassette_carries_its_environment_provenance(path):
    """Exact structural asserts stay exact, but each cassette records WHERE its
    determinism came from (model, endpoint, temp, seed, prompt hashes). A
    byte-identity break then reads 'environment changed', not 'engine
    regression' — hosted APIs do not guarantee stable numerics at temp 0."""
    env = json.loads(path.read_text())["environment"]
    for key in (
        "judge_model",
        "judge_endpoint",
        "temperature",
        "judge_seed",
        "tier1_judge_prompt_hash",
        "tier1_rubric",
        "recorded_at",
    ):
        assert key in env, key
    # The cross-pass comparability gate: A/B/C share one tier1 prompt version,
    # the proposal cell is a DIFFERENT one — never diff their scores directly.
    expected = "pre-split" if path.stem == "report_20" else "split"
    assert env["tier1_rubric"] == expected


def test_corpus_has_the_four_recorded_cells():
    assert [p.stem for p in _CORPUS] == [
        "report_17",  # A f5ab0db4
        "report_18",  # B 76f71161
        "report_19",  # C 5276bf04
        "report_20",  # proposal 0a9f08e6
    ]


def test_abc_cells_carry_the_requirement_divergence_and_anchored_form():
    """The A/B/C cells (split tier1) each recorded: requirement_provenance
    divergence (docx vs pdf) AND a form defect Localized at think — the
    docx/PDF/md chain reconciled. The proposal cell (pre-split tier1) has
    neither: a known corpus gap, not a regression."""
    for stem in ("report_17", "report_18", "report_19"):
        payload, findings, defects = _load(
            Path(__file__).parent / "fixtures" / "corpus" / f"{stem}.json"
        )
        div = [f for f in findings if f.kind == "requirement_provenance"]
        assert len(div) == 1, stem
        assert div[0].data["values"] == ["docx", "pdf"], stem
        form = [d for d in defects if d.kind == "form"]
        assert len(form) == 1 and type(form[0].origin).__name__ == "Localized", stem

    _p, findings, defects = _load(
        Path(__file__).parent / "fixtures" / "corpus" / "report_20.json"
    )
    assert not any(f.kind == "requirement_provenance" for f in findings)
    assert not any(d.kind == "form" for d in defects)
