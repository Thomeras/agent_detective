"""Redis Streams publisher seam (build spec section 4.1).

Messages are JSON documents with ``schema_version: 1``. They are XADDed as a
single ``data`` field holding the serialized JSON, so a stream entry looks
like::

    {"data": "{\"schema_version\": 1, \"graph_id\": \"...\", ...}"}

Consumers (worker M4) read the ``data`` field and parse it as JSON.
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class StreamPublisher(Protocol):
    """Async seam for stream publishing; faked in tests."""

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class RedisStreamPublisher:
    def __init__(self, client: "object") -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisStreamPublisher":
        from redis import asyncio as aioredis

        return cls(aioredis.from_url(url))

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str:
        entry_id = await self._client.xadd(stream, {"data": json.dumps(message)})
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    async def ping(self) -> None:
        await self._client.ping()

    async def close(self) -> None:
        await self._client.aclose()
