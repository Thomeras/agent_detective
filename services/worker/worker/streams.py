"""Redis Streams seams (build spec section 4.1).

Messages are JSON documents with ``schema_version: 1`` XADDed as a single
``data`` field (the same envelope the ingest finalizer uses), so a stream entry
looks like ``{"data": "{\\"schema_version\\": 1, ...}"}``.

Two protocols split the roles: ``StreamPublisher`` (XADD) and
``StreamConsumer`` (consumer-group create/read/ack plus the XPENDING/XCLAIM
primitives the dead-letter reaper needs). Both are faked in tests. The
``reap_dead_letters`` helper is pure orchestration over the protocols: it moves
messages redelivered more than ``max_deliveries`` times to ``<stream>.dlq``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from .types import PendingEntry, StreamMessage

logger = logging.getLogger(__name__)

# Suffix for the dead-letter stream of any source stream (spec 4.1).
DLQ_SUFFIX = ".dlq"


class StreamPublisher(Protocol):
    """Async seam for stream publishing; faked in tests."""

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class StreamConsumer(Protocol):
    """Async seam for consumer-group reads; faked in tests."""

    async def ensure_group(self, stream: str, group: str) -> None:
        """Create the group (and stream) if missing; existing group is a no-op."""
        ...

    async def read(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[StreamMessage]:
        """XREADGROUP new (``>``) messages for one consumer."""
        ...

    async def ack(self, stream: str, group: str, message_id: str) -> None: ...

    async def pending(
        self, stream: str, group: str, min_idle_ms: int, count: int
    ) -> list[PendingEntry]:
        """XPENDING entries idle >= ``min_idle_ms`` with their delivery counts."""
        ...

    async def claim(
        self, stream: str, group: str, consumer: str, message_id: str, min_idle_ms: int
    ) -> StreamMessage | None:
        """XCLAIM one message to ``consumer``; None if it no longer exists."""
        ...


def _decode(fields: dict[Any, Any]) -> dict[str, Any]:
    """Decode a raw XREADGROUP field mapping into the JSON payload dict."""
    raw = fields.get(b"data") if b"data" in fields else fields.get("data")
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_str(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisStreams:
    """redis-py asyncio implementation of both stream protocols.

    ``from_url`` builds the client without connecting (redis-py connects
    lazily), so importing a module that constructs one has no network side
    effect.
    """

    def __init__(self, client: "object") -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisStreams":
        from redis import asyncio as aioredis

        return cls(aioredis.from_url(url))

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str:
        entry_id = await self._client.xadd(stream, {"data": json.dumps(message)})
        return _as_str(entry_id)

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:  # redis.exceptions.ResponseError: BUSYGROUP
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(
        self, stream: str, group: str, consumer: str, count: int, block_ms: int
    ) -> list[StreamMessage]:
        response = await self._client.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        messages: list[StreamMessage] = []
        for _stream, entries in response or []:
            for entry_id, fields in entries:
                messages.append(StreamMessage(id=_as_str(entry_id), data=_decode(fields)))
        return messages

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        await self._client.xack(stream, group, message_id)

    async def pending(
        self, stream: str, group: str, min_idle_ms: int, count: int
    ) -> list[PendingEntry]:
        rows = await self._client.xpending_range(
            stream, group, min="-", max="+", count=count, idle=min_idle_ms
        )
        entries: list[PendingEntry] = []
        for row in rows or []:
            entries.append(
                PendingEntry(
                    id=_as_str(row["message_id"]),
                    delivery_count=int(row["times_delivered"]),
                )
            )
        return entries

    async def claim(
        self, stream: str, group: str, consumer: str, message_id: str, min_idle_ms: int
    ) -> StreamMessage | None:
        claimed = await self._client.xclaim(stream, group, consumer, min_idle_ms, [message_id])
        for entry_id, fields in claimed or []:
            return StreamMessage(id=_as_str(entry_id), data=_decode(fields))
        return None

    async def ping(self) -> None:
        await self._client.ping()

    async def close(self) -> None:
        await self._client.aclose()


async def reap_dead_letters(
    consumer: StreamConsumer,
    publisher: StreamPublisher,
    stream: str,
    group: str,
    reaper_name: str,
    max_deliveries: int,
    min_idle_ms: int,
) -> int:
    """Move poison messages (delivered > ``max_deliveries`` times) to the DLQ.

    Returns the number of messages moved. A message is claimed by the reaper,
    republished to ``<stream>.dlq`` with its original id, then acked on the
    source group so it stops being redelivered.
    """
    moved = 0
    for entry in await consumer.pending(stream, group, min_idle_ms, count=100):
        if entry.delivery_count <= max_deliveries:
            continue
        message = await consumer.claim(stream, group, reaper_name, entry.id, min_idle_ms)
        if message is None:
            await consumer.ack(stream, group, entry.id)
            continue
        await publisher.xadd_json(
            stream + DLQ_SUFFIX,
            {
                "schema_version": 1,
                "original_stream": stream,
                "original_id": entry.id,
                "delivery_count": entry.delivery_count,
                "payload": message.data,
            },
        )
        await consumer.ack(stream, group, entry.id)
        moved += 1
        logger.warning(
            "dead-lettered %s from %s after %d deliveries",
            entry.id,
            stream,
            entry.delivery_count,
        )
    return moved
