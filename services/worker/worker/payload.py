"""One typed view of a node's payload, built once and shared by every check.

Until now each deterministic check re-parsed the raw text for itself. That is
duplicated work, but far worse, it is duplicated *judgement*: every check has to
decide independently what a number is, where a table starts and whether a comma
is a separator or a decimal point. Get that wrong in one place and the check
does not merely miss — it accuses a node that did nothing.

Which is exactly what happened. Scanning ``2026-06,4380,1.8,44`` as one string,
the comma reads as the Czech decimal mark and ``06,4380`` collapses into
6.4380; every input figure was then corrupted, so every output figure looked
underivable and a blameless node came back as the origin. A cell can hold one
number, so splitting on the delimiter FIRST removes the ambiguity rather than
guessing at it — and doing that once, here, means no future check can reopen the
question and get a different answer.

Deliberately NOT a schema: this is a *view*, not validation. It never rejects
anything and never raises. A payload that is prose comes back as prose with no
tables, which is a fact about the payload, not a failure to parse it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

# A figure: optional sign, digits, optional thousands grouping, optional
# decimal part. Applied per CELL, never across a delimited line.
_NUMBER_RE = re.compile(
    r"(?<![\w.])-?\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?(?![\w])"
    r"|(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w])"
)

# Order matters: a semicolon-delimited row that also contains commas is a
# semicolon table, and splitting it on commas would shred its cells.
_DELIMITERS = (";", "\t", "|", ",")

_MIN_DELIMS_PER_ROW = 2


@dataclass(frozen=True)
class Table:
    """A delimited block: the delimiter that held it, and its rows as cells."""

    delimiter: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def header(self) -> tuple[str, ...]:
        return self.rows[0] if self.rows else ()


@dataclass(frozen=True)
class Payload:
    """The normalized view. ``text`` stays available — some checks legitimately
    want the prose, and hiding it would push them back into re-parsing."""

    text: str
    tables: tuple[Table, ...]
    numbers: tuple[Decimal, ...]

    @property
    def is_tabular(self) -> bool:
        return bool(self.tables)


def parse_number(token: str) -> Decimal | None:
    """The ONE place a numeric token becomes a value. Public because checks that
    read a figure out of prose (a stated exchange rate) must not re-invent it."""
    cleaned = token.replace(" ", "").replace(" ", "").replace(" ", "")
    # Both separators present: the last one is the decimal mark, the other
    # groups thousands ("1.234,56" and "1,234.56" both mean the same).
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _row_delimiter(line: str) -> str | None:
    for delim in _DELIMITERS:
        if line.count(delim) >= _MIN_DELIMS_PER_ROW:
            return delim
    return None


def _cells(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return [line]
    # Markdown tables lead and trail with the delimiter, which yields empty
    # edge cells; they carry nothing and would only dilute counts.
    return [c.strip() for c in line.split(delimiter)]


def _tables(text: str) -> tuple[Table, ...]:
    """Consecutive lines sharing one delimiter, grouped into blocks.

    A block needs two rows: a single delimited line is as likely to be prose
    with punctuation as it is to be data.
    """
    tables: list[Table] = []
    current_delim: str | None = None
    current: list[tuple[str, ...]] = []

    def flush() -> None:
        if current_delim is not None and len(current) >= 2:
            tables.append(Table(current_delim, tuple(current)))
        current.clear()

    for line in text.splitlines():
        delim = _row_delimiter(line)
        if delim is None or (current_delim is not None and delim != current_delim):
            flush()
            current_delim = delim
            if delim is not None:
                current.append(tuple(_cells(line, delim)))
            continue
        current_delim = delim
        current.append(tuple(_cells(line, delim)))
    flush()
    return tuple(tables)


def _numbers(text: str) -> tuple[Decimal, ...]:
    out: list[Decimal] = []
    for line in text.splitlines():
        for cell in _cells(line, _row_delimiter(line)):
            for match in _NUMBER_RE.finditer(cell):
                value = parse_number(match.group())
                if value is not None:
                    out.append(value)
    return tuple(out)


@lru_cache(maxsize=512)
def _normalize_cached(text: str) -> Payload:
    return Payload(text=text, tables=_tables(text), numbers=_numbers(text))


def normalize(text: str | None) -> Payload:
    """The typed view of one payload. Cached: a node's input is also its
    predecessor's output, so the same string is normalized twice per edge."""
    if not isinstance(text, str):
        return Payload(text="", tables=(), numbers=())
    return _normalize_cached(text)
