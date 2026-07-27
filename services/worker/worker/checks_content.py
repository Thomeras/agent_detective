"""Deterministic content checks (docs/deterministic-signals.md, A2 family).

Pure functions over deliverable/output text and registered check rules. Every
check emits named signals ``{name, severity, detail, basis}``; identity
(``run_id``/``agent``/``provenance``) is stamped by the caller. All parsing is
tolerant by design: malformed rules or payloads yield no signal, never an
exception — a broken rule row must not take the whole analysis down.

Signals emitted here:

- ``missing_required_section``  (fail) — a registered required section is absent;
- ``numeric_invariant_breach``  (fail) — sum over an items path != declared total;
- ``unit_inconsistency``        (warn) — output switches currency family vs input;
- ``temporal_invariant_breach`` (warn) — end/deadline before start, or deadline
  already in the past at run start;
- ``language_mismatch``         (fail) — langdetect top language contradicts the
  contract's expected language with high confidence.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException
from .narrative import signal

# langdetect is probabilistic by default; a fixed seed makes it deterministic
# (design contract: deterministic signals must be reproducible).
DetectorFactory.seed = 0

SIGNAL_MISSING_REQUIRED_SECTION = "missing_required_section"
SIGNAL_NUMERIC_INVARIANT_BREACH = "numeric_invariant_breach"
SIGNAL_UNIT_INCONSISTENCY = "unit_inconsistency"
SIGNAL_TEMPORAL_INVARIANT_BREACH = "temporal_invariant_breach"
SIGNAL_LANGUAGE_MISMATCH = "language_mismatch"


def _norm(value: object) -> str:
    """Casefolded, unicode-NFC string (mirrors worker/scoring.py ``_norm``)."""
    return unicodedata.normalize("NFC", str(value)).strip().casefold()


def _parse_json(text: str | None) -> object | None:
    """Parse JSON, tolerating prose wrapping a single ``{...}`` object."""
    if not text or not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                return None
    return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# required_section
# ---------------------------------------------------------------------------


def section_present(text: str | None, rule: dict) -> bool | None:
    """Does ``text`` contain the registered section? ``None`` = malformed rule.

    Match kinds:
    - ``substring`` — normalized (or case-sensitive NFC) containment;
    - ``word_prefix`` — any WORD in the text starts with the normalized
      pattern. This is the Czech-friendly kind: inflected forms share a stem
      ("rozpočt" matches "rozpočet", "rozpočtová", "rozpočtu"), where a full
      -word substring would false-fail on a document that genuinely contains
      the section under a different inflection;
    - ``regex`` — NFC haystack, IGNORECASE unless case_sensitive.
    """
    if not isinstance(rule, dict):
        return None
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None
    match_kind = rule.get("match")
    if match_kind not in ("substring", "word_prefix", "regex"):
        return None
    case_sensitive = rule.get("case_sensitive") is True
    haystack_raw = text if isinstance(text, str) else ""
    if match_kind == "substring":
        if case_sensitive:
            return unicodedata.normalize("NFC", pattern) in unicodedata.normalize(
                "NFC", haystack_raw
            )
        return _norm(pattern) in _norm(haystack_raw)
    if match_kind == "word_prefix":
        stem = _norm(pattern)
        return any(w.startswith(stem) for w in re.findall(r"\w+", _norm(haystack_raw)))
    try:  # regex
        flags = 0 if case_sensitive else re.IGNORECASE
        haystack = unicodedata.normalize("NFC", haystack_raw)
        return re.search(pattern, haystack, flags) is not None
    except re.error:
        return None  # invalid registered regex: rule is broken, not the run


def required_section_signals(
    text: str | None, rules: list[dict], *, subject: str = "the deliverable text"
) -> list[dict]:
    """One ``missing_required_section`` fail per rule whose section is absent.

    ``rules`` are CheckRule.spec dicts of kind ``required_section``:
    ``{"name": str, "match": "substring"|"word_prefix"|"regex", "pattern": str,
    "case_sensitive": bool = false}``. Malformed rules are skipped (no signal).
    ``text=None`` counts as an empty deliverable — required sections are then
    genuinely missing, so the rules still fire.

    ``subject`` names WHAT text was checked, and lands verbatim in the basis:
    the check runs per node in scoring ("this node's own output") and once on
    the deliverable in tier1 — a basis claiming "the deliverable text" for a
    per-node check would be a false statement in the evidence.
    """
    if not isinstance(rules, list):
        return []

    signals: list[dict] = []
    for rule in rules:
        found = section_present(text, rule)
        if found is None or found:
            continue
        pattern = rule.get("pattern")
        match_kind = rule.get("match")
        name = rule.get("name")
        name = name if isinstance(name, str) and name.strip() else pattern
        signals.append(
            signal(
                SIGNAL_MISSING_REQUIRED_SECTION, "fail",
                "required_section_missing",
                section=name, match_kind=match_kind, pattern=pattern,
                subject=subject,
            )
            # The section's identity as a TOP-LEVEL field too: the engine keys
            # the finding's fact_key on it so the deliverable-absent measurement
            # reconciles against the producers-present one.
            | {"section": name}
        )
    return signals


# ---------------------------------------------------------------------------
# sum invariant
# ---------------------------------------------------------------------------


def _resolve_dot_path(obj: object, path: str) -> object | None:
    node = obj
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def sum_invariant_signals(output_text: str | None, rules: list[dict]) -> list[dict]:
    """``numeric_invariant_breach`` when ``sum(items[].field)`` drifts from the
    declared total beyond tolerance.

    ``rules`` spec: ``{"name", "items_path": "items[].price" (single level:
    key of a list of dicts + numeric field), "total_path": "total" (dot path
    to a scalar), "tolerance": float = 0.01}``. Non-JSON output or an
    unresolvable path means the rule simply does not apply — no signal.
    """
    if not isinstance(rules, list):
        return []
    parsed = _parse_json(output_text)
    if not isinstance(parsed, dict):
        return []

    signals: list[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        items_path = rule.get("items_path")
        total_path = rule.get("total_path")
        if not isinstance(items_path, str) or not isinstance(total_path, str):
            continue
        parts = items_path.split("[].")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        list_key, field = parts

        items = _resolve_dot_path(parsed, list_key)
        if not isinstance(items, list):
            continue
        values = [
            item[field]
            for item in items
            if isinstance(item, dict) and _is_number(item.get(field))
        ]
        if items and not values:
            continue  # the numeric field never appears: path does not apply

        total = _resolve_dot_path(parsed, total_path)
        if not _is_number(total):
            continue

        tolerance = rule.get("tolerance")
        tolerance = tolerance if _is_number(tolerance) and tolerance >= 0 else 0.01
        name = rule.get("name")
        name = name if isinstance(name, str) and name.strip() else items_path

        total_sum = sum(values)
        if abs(total_sum - total) > tolerance:
            signals.append(
                signal(
                    SIGNAL_NUMERIC_INVARIANT_BREACH, "fail",
                    "sum_invariant_breach",
                    items_path=items_path, total_sum=total_sum,
                    total_path=total_path, total=total, rule_name=name,
                    tolerance=tolerance,
                )
            )
    return signals


# ---------------------------------------------------------------------------
# unit (currency family) consistency
# ---------------------------------------------------------------------------

# Word-boundary tokens per currency family. Alphabetic tokens are matched with
# word boundaries over casefolded text; bare symbols are unambiguous enough to
# match anywhere.
CURRENCY_FAMILY_TOKENS: dict[str, tuple[str, ...]] = {
    "CZK": ("czk", "kč"),
    "EUR": ("eur", "€"),
    "USD": ("usd", "$"),
    "GBP": ("gbp", "£"),
}

_SYMBOLS = {"€", "$", "£"}


def _currency_families(text: str) -> set[str]:
    lowered = _norm(text)
    found: set[str] = set()
    for family, tokens in CURRENCY_FAMILY_TOKENS.items():
        for token in tokens:
            if token in _SYMBOLS:
                if token in lowered:
                    found.add(family)
                    break
            elif re.search(rf"(?<!\w){re.escape(token)}(?!\w)", lowered):
                found.add(family)
                break
    return found


def unit_inconsistency_signals(
    input_text: str | None, output_text: str | None
) -> list[dict]:
    """``unit_inconsistency`` (warn — deliberately, this is a heuristic) when
    the input uses exactly one currency family and the output uses a different
    one while never mentioning the input's family.
    """
    if not isinstance(input_text, str) or not isinstance(output_text, str):
        return []
    input_families = _currency_families(input_text)
    if len(input_families) != 1:
        return []
    (input_family,) = input_families
    output_families = _currency_families(output_text)
    if input_family in output_families:
        return []
    others = sorted(output_families)
    if not others:
        return []
    other_label = ", ".join(others)
    return [
        signal(
            SIGNAL_UNIT_INCONSISTENCY, "warn", "currency_family_mismatch",
            input_family=input_family, output_families=other_label,
        )
    ]


# ---------------------------------------------------------------------------
# temporal invariants
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?"
)


def _parse_iso(value: object) -> datetime | None:
    """Tolerant ISO-8601 prefix parse: YYYY-MM-DD[THH:MM[:SS]] (+ ignored tail)."""
    if not isinstance(value, str):
        return None
    match = _ISO_RE.match(value)
    if not match:
        return None
    year, month, day, hh, mm, ss = match.groups()
    try:
        return datetime(
            int(year), int(month), int(day), int(hh or 0), int(mm or 0), int(ss or 0)
        )
    except ValueError:
        return None


def temporal_invariant_signals(
    output_text: str | None, *, run_started_at: datetime | None
) -> list[dict]:
    """``temporal_invariant_breach`` (warn) on ordered-date violations.

    Walks the JSON-parsed output. At each dict level, sibling keys containing
    ``start`` are paired with siblings containing ``end``/``deadline``; when
    both hold ISO dates and end < start, a signal fires. Independently, any key
    containing ``deadline``/``due`` whose date is strictly before
    ``run_started_at.date()`` fires a 'deadline in the past' signal.
    Non-JSON output -> ``[]``.
    """
    parsed = _parse_json(output_text)
    if parsed is None:
        return []
    run_date: date | None = (
        run_started_at.date() if isinstance(run_started_at, datetime) else None
    )

    signals: list[dict] = []
    seen: set[tuple] = set()

    def emit(code: str, **params) -> None:
        key = (code, tuple(sorted((k, str(v)) for k, v in params.items())))
        if key in seen:
            return
        seen.add(key)
        signals.append(
            signal(SIGNAL_TEMPORAL_INVARIANT_BREACH, "warn", code, **params)
        )

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return

        dated = {
            key: parsed_dt
            for key, value in node.items()
            if isinstance(key, str) and (parsed_dt := _parse_iso(value)) is not None
        }
        start_keys = [k for k in dated if "start" in k.casefold()]
        end_keys = [
            k
            for k in dated
            if "end" in k.casefold() or "deadline" in k.casefold()
        ]
        for start_key in start_keys:
            for end_key in end_keys:
                if end_key == start_key:
                    continue
                start_dt, end_dt = dated[start_key], dated[end_key]
                if end_dt < start_dt:
                    emit(
                        "date_order_violated",
                        start_key=start_key, start=start_dt.isoformat(),
                        end_key=end_key, end=end_dt.isoformat(),
                    )
        if run_date is not None:
            for key, value_dt in dated.items():
                lowered = key.casefold()
                if ("deadline" in lowered or "due" in lowered) and (
                    value_dt.date() < run_date
                ):
                    emit(
                        "deadline_in_past",
                        key=key, date=value_dt.date().isoformat(),
                        run_date=run_date.isoformat(),
                    )
        for value in node.values():
            walk(value)

    walk(parsed)
    return signals


# ---------------------------------------------------------------------------
# language mismatch
# ---------------------------------------------------------------------------

_MIN_LEAF_CHARS = 40
_LANG_CONFIDENCE = 0.90


def _string_leaves(node: object, out: list[str]) -> None:
    if isinstance(node, str):
        if len(node) > _MIN_LEAF_CHARS:
            out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _string_leaves(value, out)
    elif isinstance(node, list):
        for item in node:
            _string_leaves(item, out)


def language_mismatch_signals(
    expected_lang: str | None, output_text: str | None, *, min_chars: int = 120
) -> list[dict]:
    """``language_mismatch`` (fail) when langdetect's top language contradicts
    the contract's expected 2-letter code with probability >= 0.90.

    The caller resolves ``expected_lang`` from contract params
    (lang/language/locale). JSON outputs contribute only their string leaves
    longer than 40 chars (numbers/keys would poison detection); short texts
    (< ``min_chars``) are skipped — too little evidence for a fail signal.
    """
    if not isinstance(expected_lang, str) or not expected_lang.strip():
        return []
    if not isinstance(output_text, str):
        return []
    expected = _norm(expected_lang)[:2]
    if len(expected) != 2:
        return []

    parsed = _parse_json(output_text)
    if parsed is not None and isinstance(parsed, (dict, list)):
        leaves: list[str] = []
        _string_leaves(parsed, leaves)
        candidate = "\n".join(leaves)
    else:
        candidate = output_text
    if len(candidate) < min_chars:
        return []

    try:
        ranked = detect_langs(candidate)
    except LangDetectException:
        return []
    if not ranked:
        return []
    top = max(ranked, key=lambda item: item.prob)
    top_lang = str(top.lang).casefold()
    if top_lang.split("-")[0] == expected or top.prob < _LANG_CONFIDENCE:
        return []
    return [
        {
            "name": SIGNAL_LANGUAGE_MISMATCH,
            "severity": "fail",
            "detail": (
                f"expected '{expected}' but detected '{top_lang}' (p={top.prob:.2f})"
            ),
            "basis": f"langdetect over {len(candidate)} chars of output text",
        }
    ]
