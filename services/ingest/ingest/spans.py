"""Raw span sink: flattens OTLP spans into ClickHouse ``otel_spans`` rows.

The sink is a thin async protocol so tests capture rows in memory. The real
implementation uses clickhouse-connect (synchronous) on a worker thread.
Column names match docker/clickhouse/init.sql exactly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from .types import SpanRow

OTEL_SPANS_COLUMNS = [
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "start_time",
    "end_time",
    "attributes",
    "status_code",
    "resource_attributes",
]

# otel_spans timestamps are NOT NULL; spans without a time (never finished)
# are stored at the epoch so the raw row is still kept.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_unix_nanos(value: Any) -> datetime | None:
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        return None
    seconds, ns = divmod(nanos, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=ns // 1000)


def span_row(span: dict[str, Any]) -> SpanRow | None:
    """Build one ``otel_spans`` row from a flattened OTLP span dict.

    Returns None for spans without trace/span identity (unusable even as raw
    data). Attribute and status payloads are stored as-is (raw JSON).
    """
    trace_id = span.get("traceId")
    span_id = span.get("spanId")
    if not trace_id or not span_id:
        return None
    parent = span.get("parentSpanId") or ""
    status = span.get("status")
    status_code = ""
    if isinstance(status, dict):
        code = status.get("code")
        status_code = "" if code is None else str(code)
    return SpanRow(
        trace_id=str(trace_id),
        span_id=str(span_id),
        parent_span_id=str(parent),
        name=str(span.get("name") or ""),
        kind="" if span.get("kind") is None else str(span.get("kind")),
        start_time=_parse_unix_nanos(span.get("startTimeUnixNano")) or _EPOCH,
        end_time=_parse_unix_nanos(span.get("endTimeUnixNano")) or _EPOCH,
        attributes=json.dumps(span.get("attributes") or []),
        status_code=status_code,
        resource_attributes=json.dumps(span.get("resource_attributes") or {}),
    )


def mappable_span(row: SpanRow) -> dict[str, Any]:
    """Reconstruct a ``map_spans``-consumable span dict from a stored row.

    Inverse of ``span_row`` up to the storage sentinels: the epoch stands in
    for a missing timestamp (otel_spans timestamps are NOT NULL) and kind /
    status codes were stringified — both must be undone here or the mapper
    would see runs that "ended in 1970" and unparseable kinds.
    """
    span: dict[str, Any] = {
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "name": row.name,
        "attributes": json.loads(row.attributes) if row.attributes else [],
        "resource_attributes": (
            json.loads(row.resource_attributes) if row.resource_attributes else {}
        ),
    }
    if row.parent_span_id:
        span["parent_span_id"] = row.parent_span_id
    if row.kind:
        span["kind"] = int(row.kind) if row.kind.isdigit() else row.kind
    if row.start_time != _EPOCH:
        span["start_time"] = row.start_time.isoformat()
    if row.end_time != _EPOCH:
        span["end_time"] = row.end_time.isoformat()
    if row.status_code:
        code = row.status_code
        span["status"] = {"code": int(code) if code.isdigit() else code}
    return span


def _dedupe_latest(rows: list[SpanRow]) -> list[SpanRow]:
    """One row per (trace_id, span_id): raw storage is append-only, so a
    redelivered span exists twice; keep the row with the latest end time."""
    by_id: dict[tuple[str, str], SpanRow] = {}
    for row in rows:
        key = (row.trace_id, row.span_id)
        prev = by_id.get(key)
        if prev is None or row.end_time > prev.end_time:
            by_id[key] = row
    return list(by_id.values())


class SpanSink(Protocol):
    """Async seam for raw span storage; faked in tests."""

    async def insert_spans(self, rows: list[SpanRow]) -> None: ...

    async def select_spans(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        """All stored spans of the given traces as map_spans-consumable dicts,
        deduplicated per span id. Feeds the finalization re-map."""
        ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class ClickHouseSpanSink:
    """clickhouse-connect backed sink (sync client on a worker thread).

    Client construction connects (clickhouse-connect probes ``version()`` on
    ``get_client``), so it is deferred to first use. Building the sink from a
    URL therefore has no network side effect, keeping module import safe.
    """

    def __init__(self, client: "object | None" = None, url: str | None = None) -> None:
        self._client = client
        self._url = url
        self._schema_ensured = False

    @classmethod
    def from_url(cls, url: str) -> "ClickHouseSpanSink":
        return cls(url=url)

    def _get_client(self) -> "object":
        if self._client is None:
            import clickhouse_connect

            parts = urlsplit(self._url or "")
            self._client = clickhouse_connect.get_client(
                host=parts.hostname or "localhost",
                port=parts.port or (8443 if parts.scheme == "https" else 8123),
                secure=parts.scheme == "https",
            )
        return self._client

    def _ensure_schema(self) -> None:
        """Idempotent add of columns introduced after a deployment's init.sql
        ran (ClickHouse has no migration chain here; ADD COLUMN IF NOT EXISTS
        is the whole upgrade)."""
        if self._schema_ensured:
            return
        self._get_client().command(
            "ALTER TABLE otel_spans"
            " ADD COLUMN IF NOT EXISTS resource_attributes String DEFAULT '{}'"
        )
        self._schema_ensured = True

    async def insert_spans(self, rows: list[SpanRow]) -> None:
        if not rows:
            return
        data = [
            [
                r.trace_id,
                r.span_id,
                r.parent_span_id,
                r.name,
                r.kind,
                r.start_time,
                r.end_time,
                r.attributes,
                r.status_code,
                r.resource_attributes,
            ]
            for r in rows
        ]

        def _insert() -> None:
            self._ensure_schema()
            self._get_client().insert("otel_spans", data, column_names=OTEL_SPANS_COLUMNS)

        await asyncio.to_thread(_insert)

    async def select_spans(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        if not trace_ids:
            return []

        def _select() -> list[SpanRow]:
            self._ensure_schema()
            result = self._get_client().query(
                "SELECT trace_id, span_id, parent_span_id, name, kind,"
                " start_time, end_time, attributes, status_code, resource_attributes"
                " FROM otel_spans WHERE trace_id IN {tids:Array(String)}",
                parameters={"tids": trace_ids},
            )
            rows = []
            for r in result.result_rows:
                # DateTime64 comes back naive; the column is UTC by contract.
                start, end = (
                    t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)
                    for t in (r[5], r[6])
                )
                rows.append(
                    SpanRow(
                        trace_id=r[0], span_id=r[1], parent_span_id=r[2], name=r[3],
                        kind=r[4], start_time=start, end_time=end, attributes=r[7],
                        status_code=r[8], resource_attributes=r[9],
                    )
                )
            return rows

        rows = await asyncio.to_thread(_select)
        return [mappable_span(row) for row in _dedupe_latest(rows)]

    async def ping(self) -> None:
        await asyncio.to_thread(lambda: self._get_client().command("SELECT 1"))

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
