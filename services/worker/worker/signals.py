"""Deterministic artifact-integrity signals (docs/deterministic-signals.md, A1).

The worker cannot open files — they live on the instrumented host. The
instrumentation opens each artifact at flush time and ships an integrity record
OUT-OF-BAND as a span attribute (``agent_detective.artifact_meta``), which
ingest lands verbatim in ``agent_runs.artifact_meta``:

    [{"path": "output/report.md", "size": 12345, "sha256": "ab12…",
      "declared_ext": "md", "detected_kind": "text", "parse_ok": true,
      "nonempty": true}]

Out-of-band is load-bearing, not cosmetic: an earlier revision parsed
``[artifact_meta ...]`` blocks out of the PAYLOAD TEXT, and adversarial review
showed that document content can quote or forge such a block — forcing a false
deterministic ``bad`` on a healthy run, or masking a genuinely corrupt artifact.
Span attributes cannot be injected by document content, so the attribute is the
only authoritative source; payload text is never consulted.

This module PARSES that attribute and re-checks it — pure functions, no I/O,
no LLM. Every check emits a named signal ``{name, severity, detail, basis}``;
callers stamp identity (``run_id``/``agent``/``provenance``) at their own
level (NodeScore for node scoring, the worker post-serialize for the graph
level). Parsing is tolerant by design: a malformed attribute yields no signals,
never an exception — a broken emitter must not take the whole analysis down.
"""

from __future__ import annotations

import hashlib
import json

SIGNAL_ARTIFACT_INTEGRITY_FAIL = "artifact_integrity_fail"


def check_rules_fingerprint(rules, *, min_artifact_bytes: int) -> str:
    """Deterministic fingerprint of the rule set a deterministic verdict was
    computed under (12 hex chars).

    Stored on ``tier1_verdicts`` so a later reconciliation that finds the
    verdict's basis non-reproducible can tell WHY with certainty: a different
    fingerprint = the registered rules changed between tier1 and re-analysis;
    the same fingerprint = the rules are identical, so the payload/artifact
    itself evaluates differently — genuine representation divergence.

    Canonical form: sorted list of {kind, agent_name, graph_type, spec} (row
    ids excluded — re-registering an identical rule must not change the
    fingerprint) plus the settings the checks read (min_artifact_bytes).
    """
    canonical = {
        "rules": sorted(
            (
                {
                    "kind": r.kind,
                    "agent_name": r.agent_name,
                    "graph_type": r.graph_type,
                    "spec": r.spec,
                }
                for r in (rules or [])
            ),
            key=lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False),
        ),
        "min_artifact_bytes": min_artifact_bytes,
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

# Expected detect_kind per declared extension. Deliberately a small DUPLICATED
# table: the emitter (detective_sdk) and this checker are different parties —
# sharing one table would let a single bug pass both sides silently.
_EXT_KIND: dict[str, str] = {
    "docx": "zip",
    "xlsx": "zip",
    "pptx": "zip",
    "pdf": "pdf",
    "md": "text",
    "txt": "text",
    "html": "text",
    "htm": "text",
    "json": "text",
    "csv": "text",
}


def parse_artifact_meta(meta_text: str | None) -> list[dict]:
    """Parse the ``agent_detective.artifact_meta`` attribute value.

    Accepts a JSON array of meta dicts (the contract) or a single dict (wrapped
    for tolerance). Entries without a usable ``path`` string get ``path: "?"``
    rather than being dropped — a check result should not vanish because the
    emitter forgot a field. Malformed JSON -> ``[]``; never raises.
    """
    if not meta_text:
        return []
    try:
        parsed = json.loads(meta_text)
    except ValueError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    results: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        results.append(
            {
                "path": path if isinstance(path, str) and path.strip() else "?",
                "meta": entry,
            }
        )
    return results


def _declared_ext(path: str, meta: dict) -> str | None:
    """Declared extension: the meta's own claim first, then the path's."""
    declared = meta.get("declared_ext")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lstrip(".").lower()
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].strip().lower() or None


def _fail(detail: str, basis: str) -> dict:
    return {
        "name": SIGNAL_ARTIFACT_INTEGRITY_FAIL,
        "severity": "fail",
        "detail": detail,
        "basis": basis,
    }


def artifact_integrity_signals(
    meta_text: str | None, *, min_bytes: int
) -> list[dict]:
    """Re-check every artifact meta entry; one signal per failed check (a
    single file can raise several). No/malformed meta -> ``[]``.

    Checks (design table, docs/deterministic-signals.md A1):
    - magic vs extension: declared ext maps to an expected kind that differs
      from ``detected_kind`` (skipped when the file is missing — that is its
      own failure, not a mismatch);
    - file presence: ``detected_kind == "missing"``;
    - parseability: ``parse_ok == false``;
    - non-empty: ``nonempty == false`` (its own basis — never phrased as a size
      comparison that might be false);
    - minimum size: ``size < min_bytes``.
    """
    signals: list[dict] = []
    for entry in parse_artifact_meta(meta_text):
        path = entry["path"]
        meta = entry["meta"]
        ext = _declared_ext(path, meta)
        detected = meta.get("detected_kind")
        detected = detected if isinstance(detected, str) else None

        if detected == "missing":
            signals.append(
                _fail(
                    f"declared artifact {path} does not exist",
                    "file missing at flush",
                )
            )
        elif (
            ext is not None
            and ext in _EXT_KIND
            and detected is not None
            and _EXT_KIND[ext] != detected
        ):
            signals.append(
                _fail(
                    f"declared .{ext} but content is {detected}",
                    f"magic bytes: detected_kind={detected} for {path}",
                )
            )

        if meta.get("parse_ok") is False:
            declared = f".{ext}" if ext else "artifact"
            signals.append(
                _fail(
                    f"{path} does not parse as a valid {declared} file",
                    "parse check",
                )
            )

        size = meta.get("size")
        size_val = size if isinstance(size, int) and not isinstance(size, bool) else None
        # Two distinct failures with distinct bases: the basis must state a TRUE
        # observation (a "size=5000 < min 64" claim on a nonempty=false file
        # with 5000 allocated bytes would be a false statement in the evidence).
        if meta.get("nonempty") is False:
            signals.append(
                _fail(
                    f"{path} has no content",
                    f"content check (nonempty=false, size={size_val if size_val is not None else 'unknown'})",
                )
            )
        elif size_val is not None and size_val < min_bytes:
            signals.append(
                _fail(
                    f"{path} is below the minimum plausible size",
                    f"size check (size={size_val} < min {min_bytes})",
                )
            )
    return signals
