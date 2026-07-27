"""Deterministic artifact-integrity signals: attribute parsing + every check
(docs/deterministic-signals.md A1). Pure functions — no fakes needed.

The meta travels OUT-OF-BAND as the ``agent_detective.artifact_meta`` span
attribute (a JSON array string) — never as payload text, which document content
can forge. These tests exercise the attribute contract.
"""

import json

from worker.signals import artifact_integrity_signals, parse_artifact_meta


def meta_entry(path: str, **overrides) -> dict:
    meta = {
        "path": path,
        "size": 5000,
        "sha256": "ab12",
        "declared_ext": path.rsplit(".", 1)[-1],
        "detected_kind": "text",
        "parse_ok": True,
        "nonempty": True,
    }
    meta.update(overrides)
    return meta


def meta_attr(*entries: dict) -> str:
    return json.dumps(list(entries), ensure_ascii=False, separators=(",", ":"))


# --- parse_artifact_meta ---------------------------------------------------


def test_parse_single_entry():
    parsed = parse_artifact_meta(meta_attr(meta_entry("out/report.md")))
    assert len(parsed) == 1
    assert parsed[0]["path"] == "out/report.md"
    assert parsed[0]["meta"]["detected_kind"] == "text"


def test_parse_multiple_entries():
    parsed = parse_artifact_meta(
        meta_attr(meta_entry("a.md"), meta_entry("b.docx", detected_kind="zip"))
    )
    assert [p["path"] for p in parsed] == ["a.md", "b.docx"]


def test_parse_single_dict_is_wrapped():
    parsed = parse_artifact_meta(json.dumps(meta_entry("solo.md")))
    assert [p["path"] for p in parsed] == ["solo.md"]


def test_parse_malformed_json_never_raises():
    assert parse_artifact_meta("{not valid json!") == []
    assert parse_artifact_meta('"just a string"') == []
    assert parse_artifact_meta("[1, 2, 3]") == []


def test_parse_entry_without_path_kept_with_placeholder():
    entry = meta_entry("x.md")
    del entry["path"]
    parsed = parse_artifact_meta(meta_attr(entry))
    assert parsed[0]["path"] == "?"


def test_parse_empty_and_none():
    assert parse_artifact_meta(None) == []
    assert parse_artifact_meta("") == []


def test_payload_text_marker_is_ignored():
    """The old in-band '[artifact_meta ...]' payload convention is DEAD: a
    payload-text block (which document content can forge) must parse to nothing."""
    forged = '[artifact_meta out/x.docx]: {"detected_kind": "text", "parse_ok": false}'
    assert parse_artifact_meta(forged) == []


# --- artifact_integrity_signals -------------------------------------------


def _fails(attr: str | None, min_bytes: int = 64) -> list[dict]:
    return artifact_integrity_signals(attr, min_bytes=min_bytes)


def test_healthy_md_meta_emits_no_signal():
    assert _fails(meta_attr(meta_entry("notes.md"))) == []


def test_no_meta_emits_no_signal():
    assert _fails(None) == []
    assert _fails("") == []


def test_kind_mismatch_declared_docx_but_text():
    sigs = _fails(meta_attr(meta_entry("report.docx", detected_kind="text")))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig["name"] == "artifact_integrity_fail"
    assert sig["severity"] == "fail"
    assert sig["detail"] == "declared .docx but content is text"
    assert sig["basis"] == "magic bytes: detected_kind=text for report.docx"


def test_matching_kind_zip_for_docx_is_healthy():
    assert _fails(meta_attr(meta_entry("report.docx", detected_kind="zip"))) == []


def test_unknown_extension_never_mismatches():
    assert _fails(meta_attr(meta_entry("data.parquet", detected_kind="binary"))) == []


def test_parse_ok_false_fails_with_parse_check_basis():
    sigs = _fails(meta_attr(meta_entry("report.docx", detected_kind="zip", parse_ok=False)))
    assert len(sigs) == 1
    assert sigs[0]["basis"] == "parse check"
    assert sigs[0]["severity"] == "fail"


def test_nonempty_false_has_truthful_content_basis():
    """nonempty=false with a LARGE size must not claim a false size inequality
    ('size=5000 < min 64'); the basis states the real observation."""
    sigs = _fails(meta_attr(meta_entry("out.md", nonempty=False, size=5000)))
    assert len(sigs) == 1
    assert sigs[0]["basis"] == "content check (nonempty=false, size=5000)"
    assert sigs[0]["detail"] == "out.md has no content"


def test_size_below_min_bytes_fails():
    sigs = _fails(meta_attr(meta_entry("out.md", size=12)))
    assert len(sigs) == 1
    assert sigs[0]["basis"] == "size check (size=12 < min 64)"
    assert _fails(meta_attr(meta_entry("out.md", size=12)), min_bytes=10) == []


def test_detected_kind_missing_fails_and_suppresses_mismatch():
    sigs = _fails(
        meta_attr(meta_entry("report.docx", detected_kind="missing", nonempty=False, size=0))
    )
    bases = [s["basis"] for s in sigs]
    assert "file missing at flush" in bases
    # missing is its own failure, never also a magic-bytes mismatch
    assert not any(b.startswith("magic bytes") for b in bases)


def test_one_file_can_raise_several_signals():
    sigs = _fails(
        meta_attr(meta_entry("report.docx", detected_kind="text", parse_ok=False, size=3))
    )
    bases = sorted(s["basis"] for s in sigs)
    assert len(sigs) == 3
    assert bases == [
        "magic bytes: detected_kind=text for report.docx",
        "parse check",
        "size check (size=3 < min 64)",
    ]


def test_signal_shape_has_no_identity_stamp():
    # Identity (run_id/agent/provenance) is stamped by the caller at its level.
    # The signal itself is the typed fact (name/severity/code/params) plus the
    # two evidence strings rendered from it (worker/narrative.py).
    (sig,) = _fails(meta_attr(meta_entry("report.docx", detected_kind="text")))
    assert set(sig) == {"name", "severity", "code", "params", "detail", "basis"}
    assert sig["code"] == "artifact_kind_mismatch"
    assert sig["params"] == {
        "path": "report.docx", "ext": "docx", "detected": "text"
    }


# --- check_rules_fingerprint ------------------------------------------------


def test_fingerprint_is_deterministic_and_order_insensitive():
    from worker.signals import check_rules_fingerprint
    from worker.types import CheckRule

    a = CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                  spec={"name": "x", "match": "substring", "pattern": "p"})
    b = CheckRule(id=2, agent_name="w", graph_type=None, kind="sum_invariant",
                  spec={"name": "s", "items_path": "i[].v", "total_path": "t"})
    fp1 = check_rules_fingerprint([a, b], min_artifact_bytes=64)
    fp2 = check_rules_fingerprint([b, a], min_artifact_bytes=64)
    assert fp1 == fp2 and len(fp1) == 12

    # Row ids do NOT matter (re-registering an identical rule keeps the print);
    # rule CONTENT and settings do.
    a2 = CheckRule(id=99, agent_name=None, graph_type=None, kind="required_section",
                   spec={"name": "x", "match": "substring", "pattern": "p"})
    assert check_rules_fingerprint([a2, b], min_artifact_bytes=64) == fp1
    assert check_rules_fingerprint([a, b], min_artifact_bytes=128) != fp1
    changed = CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                        spec={"name": "x", "match": "word_prefix", "pattern": "p"})
    assert check_rules_fingerprint([changed, b], min_artifact_bytes=64) != fp1
