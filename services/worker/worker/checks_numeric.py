"""Deterministic numeric fidelity (docs/deterministic-signals.md, A4).

The class of failure with no check at all until now: fluent, well-formed output
carrying wrong numbers. The judged channel reads it as good work — a boundary
adapter that ran percentages through an exchange rate scored 0.9 — because
nothing about a tidy table of wrong figures looks wrong. That is precisely the
failure a deterministic check can catch and a reader cannot.

Two checks, both pure functions over (input_text, output_text), both working on
CSV and prose rather than requiring JSON, because real payloads are rarely JSON:

- ``numeric_content_lost`` (fail) — the input carried figures and the output
  carries none at all. A node handed a table and returning prose with no
  quantity has dropped the measurable content, whatever it says.
- ``number_not_derivable`` (fail) — a figure in a TABULAR output that is neither
  present in the input nor reachable from an input figure by a constant the
  input itself states (an exchange rate, a per-hundred, a per-thousand). Either
  it was invented or it was miscomputed; both are the node's own defect and the
  input/output pair proves it without a model.

Why the second one is scoped to tabular output: a node that analyses, sums or
forecasts legitimately produces numbers that are in no sense copies of its
input, and blaming it for that would be worse than the miss. A delimited table
is the shape of a re-keyed or converted dataset, where every cell IS supposed to
trace back to something.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .narrative import signal

SIGNAL_NUMERIC_CONTENT_LOST = "numeric_content_lost"
SIGNAL_NUMBER_NOT_DERIVABLE = "number_not_derivable"

# A figure: optional sign, digits, optional thousands separators, optional
# decimal part. Deliberately not matching bare years inside words or ids.
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:[  ]\d{3})*(?:[.,]\d+)?(?![\w])|(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w])")

# "1 EUR = 24.6 CZK", "kurz 24,6", "1 USD = 22 CZK" — the constant the output is
# allowed to have applied. Only rates the INPUT states count: a constant the
# node brought with it is exactly what we cannot verify.
# Two forms, and the explicit one must be tried on its own: a single alternation
# starting at "Kurz" captured the 1 out of "Kurz: 1 EUR = 24.6 CZK" and every
# conversion then looked underivable — including the correct ones.
_RATE_FORMS = (
    re.compile(r"1\s*[A-Z]{3}\s*=\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE),
    re.compile(r"(?:kurz|rate|exchange)\s*:?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE),
)

# Scale changes that need no stated constant: percent, per-mille, basis points,
# and plain decimal shifts. A converted figure is allowed to differ by these.
_UNIVERSAL_FACTORS = (Decimal(1), Decimal(10), Decimal(100), Decimal(1000), Decimal(10000))

_MIN_INPUT_NUMBERS = 8      # below this the input is not "a table of figures"
_MIN_OUTPUT_CHARS = 200     # below this the output is too short to judge
# 0.01%. Wide enough for display rounding (rounding a converted figure to whole
# units is ~1e-7 relative, to thousands ~5e-5) and narrow enough to catch the
# real thing: the adapter's 4538580 against the correct 4533780 is 0.106% off,
# which a 0.5% tolerance swallowed whole.
_TOLERANCE = Decimal("0.0001")


def _to_decimal(token: str) -> Decimal | None:
    cleaned = token.replace(" ", "").replace(" ", "").replace(" ", "")
    # A comma is a decimal separator in these payloads; a dot may be either, but
    # "1.38" and "1,38" must both parse to the same value.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


_DELIMS = (";", "|", "\t", ",")


def _cells(line: str) -> list[str]:
    """A delimited row split into cells, or the whole line when it is prose.

    Splitting FIRST is what makes a comma safe. In `2026-06,4380,1.8,44` the
    comma is a field separator, but it is also the Czech decimal mark, and a
    scan over the raw line merged `06,4380` into 6.4380 — which corrupted every
    input figure and then reported the whole output as underivable. A cell can
    only hold one number, so per-cell parsing removes the ambiguity instead of
    guessing at it.
    """
    for delim in _DELIMS:
        if line.count(delim) >= 2:
            return line.split(delim)
    return [line]


def _numbers(text: str) -> list[Decimal]:
    out = []
    for line in text.splitlines():
        for cell in _cells(line):
            for match in _NUMBER_RE.finditer(cell):
                value = _to_decimal(match.group())
                if value is not None:
                    out.append(value)
    return out


def _stated_rates(text: str) -> list[Decimal]:
    rates = []
    for form in _RATE_FORMS:
        for match in form.finditer(text):
            value = _to_decimal(match.group(1))
            # A rate of 1 is not a conversion and would only widen what counts
            # as derivable; 1 is already among the universal factors.
            if value is not None and value > 0 and value != 1:
                rates.append(value)
    return rates


def _looks_tabular(text: str) -> bool:
    """At least two lines sharing a delimiter and a consistent column count."""
    for delim in (";", ",", "\t", "|"):
        rows = [ln for ln in text.splitlines() if ln.count(delim) >= 2]
        if len(rows) >= 2 and len({ln.count(delim) for ln in rows}) == 1:
            return True
    return False


def _close(a: Decimal, b: Decimal) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= _TOLERANCE


def numeric_content_lost_signals(
    input_text: str | None, output_text: str | None
) -> list[dict]:
    """``numeric_content_lost`` (fail) — figures in, no figures out."""
    if not isinstance(input_text, str) or not isinstance(output_text, str):
        return []
    if len(output_text.strip()) < _MIN_OUTPUT_CHARS:
        return []
    in_numbers = _numbers(input_text)
    if len(in_numbers) < _MIN_INPUT_NUMBERS:
        return []
    if _numbers(output_text):
        return []
    return [
        signal(
            SIGNAL_NUMERIC_CONTENT_LOST, "fail", "numeric_content_lost",
            input_numbers=len(in_numbers), output_chars=len(output_text.strip()),
        )
    ]


def number_not_derivable_signals(
    input_text: str | None, output_text: str | None, *, max_reported: int = 3
) -> list[dict]:
    """``number_not_derivable`` (fail) — a tabular figure traceable to nothing.

    Derivable means: present verbatim in the input, or equal to some input
    figure multiplied or divided by a rate the input states, or by a decimal
    scale factor. Everything else in a converted table is either invented or
    miscomputed.
    """
    if not isinstance(input_text, str) or not isinstance(output_text, str):
        return []
    if not _looks_tabular(output_text):
        return []
    in_numbers = _numbers(input_text)
    out_numbers = _numbers(output_text)
    if len(in_numbers) < _MIN_INPUT_NUMBERS or not out_numbers:
        return []

    factors = list(_UNIVERSAL_FACTORS) + _stated_rates(input_text)
    orphans = []
    for value in out_numbers:
        if any(_close(value, source) for source in in_numbers):
            continue
        derivable = False
        for source in in_numbers:
            for factor in factors:
                if _close(value, source * factor) or (
                    factor != 0 and _close(value, source / factor)
                ):
                    derivable = True
                    break
            if derivable:
                break
        if not derivable:
            orphans.append(value)

    if not orphans:
        return []
    shown = ", ".join(str(v) for v in orphans[:max_reported])
    return [
        signal(
            SIGNAL_NUMBER_NOT_DERIVABLE, "fail", "number_not_derivable",
            count=len(orphans), values=shown, checked=len(out_numbers),
        )
    ]
