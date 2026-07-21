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
    )


class SpanSink(Protocol):
    """Async seam for raw span storage; faked in tests."""

    async def insert_spans(self, rows: list[SpanRow]) -> None: ...

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
            ]
            for r in rows
        ]

        def _insert() -> None:
            self._get_client().insert("otel_spans", data, column_names=OTEL_SPANS_COLUMNS)

        await asyncio.to_thread(_insert)

    async def ping(self) -> None:
        await asyncio.to_thread(lambda: self._get_client().command("SELECT 1"))

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
