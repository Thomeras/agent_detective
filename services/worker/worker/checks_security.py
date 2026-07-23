"""Deterministic security scans (docs/deterministic-signals.md, A4 family).

Pure functions over payload text. Two signal names:

- ``sensitive_data_exposure``   (warn) — PII/secret material present in text;
- ``prompt_injection_signature``(warn) — known injection/exfiltration markers.

Both are heuristics, hence warn. Detected sensitive values are NEVER echoed
into the signal — the basis carries only the first 4 characters (a signal that
re-exposes the secret in the blame report would itself be an exposure).
Malformed input -> no signal, never an exception.
"""

from __future__ import annotations

import base64
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit

SIGNAL_SENSITIVE_DATA_EXPOSURE = "sensitive_data_exposure"
SIGNAL_PROMPT_INJECTION_SIGNATURE = "prompt_injection_signature"

# ---------------------------------------------------------------------------
# sensitive data
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+\d[\d \-()]{7,}\d")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Za-z0-9]{11,30}\b")
_CARD_RE = re.compile(r"(?<![\dA-Za-z])(?:\d[ -]?){12,18}\d(?![\dA-Za-z])")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_ENTROPY_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=]{32,}$")

_ENTROPY_THRESHOLD_BITS = 4.5
_ENTROPY_MIN_LEN = 32


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    rearranged = candidate[4:] + candidate[:4]
    try:
        numeric = int(
            "".join(
                str(int(ch, 36)) for ch in rearranged.upper()
            )
        )
    except ValueError:
        return False
    return numeric % 97 == 1


def _jwt_header_ok(candidate: str) -> bool:
    header_b64 = candidate.split(".", 1)[0]
    padded = header_b64 + "=" * (-len(header_b64) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(header, dict) and "alg" in header


def _shannon_bits_per_char(token: str) -> float:
    counts: dict[str, int] = {}
    for char in token:
        counts[char] = counts.get(char, 0) + 1
    length = len(token)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def sensitive_data_signals(text: str | None) -> list[dict]:
    """One ``sensitive_data_exposure`` warn per KIND of sensitive material
    found (deduped by kind). The matched value is redacted: only its first 4
    characters appear in the basis.
    """
    if not isinstance(text, str) or not text:
        return []

    hits: dict[str, str] = {}  # kind -> first matched value

    def record(kind: str, value: str) -> None:
        hits.setdefault(kind, value)

    for match in _EMAIL_RE.finditer(text):
        record("email address", match.group(0))
    for match in _PHONE_RE.finditer(text):
        if sum(ch.isdigit() for ch in match.group(0)) >= 9:
            record("phone number", match.group(0))
    for match in _IBAN_RE.finditer(text):
        if _iban_ok(match.group(0)):
            record("IBAN", match.group(0))
    for match in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            record("payment card number", match.group(0))
    for match in _AWS_KEY_RE.finditer(text):
        record("AWS access key id", match.group(0))
    for match in _PRIVATE_KEY_RE.finditer(text):
        record("private key", match.group(0))
    for match in _JWT_RE.finditer(text):
        if _jwt_header_ok(match.group(0)):
            record("JWT", match.group(0))
    for token in text.split():
        if (
            len(token) >= _ENTROPY_MIN_LEN
            and _ENTROPY_TOKEN_RE.match(token)
            and _shannon_bits_per_char(token) > _ENTROPY_THRESHOLD_BITS
        ):
            record("high-entropy token (possible secret)", token)

    return [
        {
            "name": SIGNAL_SENSITIVE_DATA_EXPOSURE,
            "severity": "warn",
            "detail": f"{kind} detected",
            "basis": (
                f"{kind} pattern match; value REDACTED "
                f"(first 4 chars '{value[:4]}…')"
            ),
        }
        for kind, value in hits.items()
    ]


# ---------------------------------------------------------------------------
# prompt injection signatures
# ---------------------------------------------------------------------------

_LITERAL_SIGNATURES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all prior",
    "disregard your instructions",
    "you are now",
    "system prompt",
    "reveal your instructions",
)

_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060-\u2064]")
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069]")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(\S+?)\s*\)")

_EXFIL_PARAM_VALUE_LEN = 64


def _has_long_query_param(url: str) -> bool:
    try:
        query = urlsplit(url).query
    except ValueError:
        return False
    if not query:
        return False
    for _key, value in parse_qsl(query, keep_blank_values=True):
        if len(value) > _EXFIL_PARAM_VALUE_LEN:
            return True
    return False


def injection_signature_signals(text: str | None) -> list[dict]:
    """One ``prompt_injection_signature`` warn per matched signature
    (deduped by signature).
    """
    if not isinstance(text, str) or not text:
        return []

    lowered = text.casefold()
    matched: list[str] = []

    for signature in _LITERAL_SIGNATURES:
        if signature in lowered:
            matched.append(signature)
    if _ZERO_WIDTH_RE.search(text):
        matched.append("zero-width characters")
    if _BIDI_RE.search(text):
        matched.append("bidi control characters")
    for match in _MD_IMAGE_RE.finditer(text):
        if _has_long_query_param(match.group(1)):
            matched.append("markdown image exfiltration URL")
            break

    return [
        {
            "name": SIGNAL_PROMPT_INJECTION_SIGNATURE,
            "severity": "warn",
            "detail": f"injection signature '{signature}' present",
            "basis": "literal/unicode pattern match",
        }
        for signature in matched
    ]
