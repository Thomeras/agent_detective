"""The worker's narrative layer — where its prose is covered, exactly once.

Same split as the engine's `test_narrative.py`: behaviour tests assert typed
records (a signal's `code`/`params`, a hypothesis's `basis_code`, a stale
`cause` code); the sentences those become are asserted here and nowhere else.

The invariants:

1. **Every code a detector emits has a template.** `render_signal_detail` /
   `render_signal_basis` degrade to the bare code, so an emitter added without a
   template renders as an identifier — this catches it.
2. **`detail` and `basis` come from ONE payload.** They used to be two
   independently written strings, which is how a basis could state an
   observation the detail contradicts (the artifact size/nonempty pair).
3. **No template calls anything "ground truth"** (§11 row 11) — a deterministic
   check says what it checked; nothing claims to be beyond dispute.
"""

import inspect
import re

import pytest

from worker import behavioral, checks_content, checks_numeric, checks_security, signals
from worker.narrative import (
    _NOTE_TEMPLATES,
    _SIGNAL_TEMPLATES,
    HYPOTHESIS_LATER_PRODUCER,
    HYPOTHESIS_REPORTED,
    HYPOTHESIS_UNRESOLVED,
    STALE_CAUSE_PAYLOAD_DIVERGED,
    STALE_CAUSE_RULES_CHANGED,
    STALE_CAUSE_UNSTAMPED,
    render_hypothesis_basis,
    render_shipped_caveat,
    render_signal_basis,
    render_signal_detail,
    render_stale_cause,
    signal,
)

# --- 1. every emitted signal code has a template -------------------------


def _emitted_codes() -> set[str]:
    """Signal codes reachable from the detectors, read off their call sites.

    Deliberately source-derived rather than hand-listed: a new detector shows up
    here the moment it is written, so the coverage claim cannot go stale.
    """
    codes: set[str] = set()
    for module in (signals, behavioral, checks_content, checks_numeric, checks_security):
        # Codes are quoted literals at the call sites (`signal(name, sev, CODE)`
        # / `_fail(CODE, ...)`), so a literal scan is enough and stays honest.
        for token in re.findall(r"""["']([a-z_]+)["']""", inspect.getsource(module)):
            if token in _SIGNAL_TEMPLATES:
                codes.add(token)
    return codes


def test_every_signal_code_the_detectors_emit_has_a_template():
    emitted = _emitted_codes()
    assert emitted, "no signal codes found in the detector sources"
    assert emitted <= set(_SIGNAL_TEMPLATES)


def test_no_signal_template_is_dead():
    """A template nothing emits is dead prose. `structured_field_drop` is
    emitted from tier2 (not one of the detector modules scanned above)."""
    unreached = set(_SIGNAL_TEMPLATES) - _emitted_codes()
    assert unreached <= {"structured_field_drop"}


def test_unknown_code_degrades_to_the_identifier():
    assert render_signal_detail("brand_new_check", {}) == "brand_new_check"
    assert render_signal_basis("brand_new_check", {}) == "brand_new_check"


def test_signal_carries_both_the_record_and_its_rendering():
    s = signal("artifact_integrity_fail", "fail", "artifact_too_small",
               path="out/x.md", size=3, min_bytes=64)
    assert s == {
        "name": "artifact_integrity_fail",
        "severity": "fail",
        "code": "artifact_too_small",
        "params": {"path": "out/x.md", "size": 3, "min_bytes": 64},
        "detail": "out/x.md is below the minimum plausible size",
        "basis": "size check (size=3 < min 64)",
    }


# --- 2. detail and basis are rendered from ONE payload -------------------


def test_empty_artifact_basis_never_states_a_size_comparison():
    """The regression this typing prevents: a file with `nonempty=false` but a
    non-zero allocated size would, under two independently written strings, get
    a "size=5000 < min 64" basis — a false statement inside the evidence."""
    s = signal("artifact_integrity_fail", "fail", "artifact_empty",
               path="out/x.md", size=5000)
    assert s["detail"] == "out/x.md has no content"
    assert s["basis"] == "content check (nonempty=false, size=5000)"
    assert "min" not in s["basis"]

    unknown = signal("artifact_integrity_fail", "fail", "artifact_empty",
                     path="out/x.md", size=None)
    assert unknown["basis"] == "content check (nonempty=false, size=unknown)"


@pytest.mark.parametrize(
    "code,params,detail,basis",
    [
        (
            "artifact_missing", {"path": "out/x.md"},
            "declared artifact out/x.md does not exist", "file missing at flush",
        ),
        (
            "artifact_kind_mismatch",
            {"path": "r.docx", "ext": "docx", "detected": "text"},
            "declared .docx but content is text",
            "magic bytes: detected_kind=text for r.docx",
        ),
        (
            "artifact_parse_failed", {"path": "r.docx", "ext": "docx"},
            "r.docx does not parse as a valid .docx file", "parse check",
        ),
        (
            "artifact_parse_failed", {"path": "blob", "ext": None},
            "blob does not parse as a valid artifact file", "parse check",
        ),
        (
            "loop_fingerprint", {"tool": "search", "calls": 3, "args_sha": "ab12"},
            "tool 'search' called 3x consecutively with identical args",
            "args_sha ab12 repeated",
        ),
        (
            "prompt_injection", {"signature": "ignore previous"},
            "injection signature 'ignore previous' present",
            "literal/unicode pattern match",
        ),
        (
            "sensitive_data", {"kind": "aws_key", "prefix": "AKIA"},
            "aws_key detected",
            "aws_key pattern match; value REDACTED (first 4 chars 'AKIA…')",
        ),
    ],
)
def test_signal_wordings(code, params, detail, basis):
    assert render_signal_detail(code, params) == detail
    assert render_signal_basis(code, params) == basis


def test_metric_outlier_formats_numbers_without_float_noise():
    s = signal("cost_outlier", "warn", "metric_outlier",
               metric="cost_usd", value=1.5, z=3.25, mean=0.5, std=0.25,
               sample_count=12)
    assert s["detail"] == "cost_usd=1.5 is 3.2σ above the rolling mean 0.5"
    assert s["basis"] == "baseline n=12, mean=0.5, std=0.25"


# --- 3. hypotheses / stale cause / caveats -------------------------------


def test_hypothesis_bases_are_keyed_by_code():
    assert render_hypothesis_basis(HYPOTHESIS_REPORTED).startswith("reported origin")
    assert render_hypothesis_basis(HYPOTHESIS_UNRESOLVED).startswith("unresolved")
    later = render_hypothesis_basis(
        HYPOTHESIS_LATER_PRODUCER,
        {"signals": ["evidence_tension", "representation_divergence"]},
    )
    assert "evidence_tension + representation_divergence" in later
    assert "later than reported" in later


def test_stale_cause_routes_to_three_different_owners():
    """The three causes are not phrasings of one fact: nobody / the operator /
    the agent integration owns each. Keying them by code is what keeps the
    routing checkable."""
    unstamped = render_stale_cause(STALE_CAUSE_UNSTAMPED)
    changed = render_stale_cause(
        STALE_CAUSE_RULES_CHANGED, {"stored": "aaa", "current": "bbb"}
    )
    diverged = render_stale_cause(
        STALE_CAUSE_PAYLOAD_DIVERGED, {"stored": "aaa", "current": "aaa"}
    )
    assert "cause unknown" in unstamped
    assert "fingerprint aaa -> bbb" in changed and "operator side" in changed
    assert "UNCHANGED (fingerprint aaa)" in diverged
    assert "AGENT" in diverged
    assert len({unstamped, changed, diverged}) == 3


def test_shipped_caveat_is_keyed_on_the_content_axis():
    """"ok in CONTENT only" over a bad content verdict is a self-contradiction;
    keying the template on the axis state makes it unwritable."""
    shipped = [{"key": "file_type", "from": "docx", "to": "md"}]
    ok = render_shipped_caveat(shipped, content="ok")
    bad = render_shipped_caveat(shipped, content="bad")
    stale = render_shipped_caveat(shipped, content="stale")

    assert ok.startswith("ok in CONTENT only")
    assert "ok in CONTENT only" not in bad
    assert bad.startswith("TWO independent faults")
    assert "ok in CONTENT only" not in stale
    assert stale.startswith("content verdict is STALE")
    # All three quote the same breach detail from the same payload.
    for rendered in (ok, bad, stale):
        assert "file_type 'md' shipped, 'docx' required" in rendered


# --- 4. the certainty taxonomy holds worker-side too ---------------------


def test_no_worker_template_calls_anything_ground_truth():
    for slug, template in _NOTE_TEMPLATES.items():
        try:
            rendered = template({"shipped": [], "signals": []})
        except Exception:
            continue
        assert "ground truth" not in rendered.lower(), slug
    for code, (detail_tpl, basis_tpl) in _SIGNAL_TEMPLATES.items():
        for tpl in (detail_tpl, basis_tpl):
            try:
                rendered = tpl({})
            except Exception:
                continue
            assert "ground truth" not in rendered.lower(), code
