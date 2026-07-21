"""Payload object storage reader behind a thin async protocol (spec 4.3).

Ingest routes large payloads to bucket ``agent-detective-payloads`` under
``payloads/{graph_id}/{run_id}/{input|output}`` and stores the reference
``s3://{bucket}/{key}`` in the ``*_overflow_ref`` column, keeping only a bounded
prefix inline. The worker needs the *full* payload for scoring/judging, so it
fetches the overflow object when a reference is present and otherwise uses the
inline column.
"""

from __future__ import annotations

import asyncio
from typing import Protocol
from urllib.parse import urlsplit


class ObjectStore(Protocol):
    """Minimal async object-storage read seam; faked in tests."""

    async def get(self, bucket: str, key: str) -> bytes:
        """Fetch the full object stored at ``bucket``/``key``."""
        ...


class MinioObjectStore:
    """MinIO-backed reader. The minio client is synchronous, so calls are
    dispatched to a worker thread to keep the event loop free. Client
    construction performs no network I/O (safe at import time)."""

    def __init__(self, client: "object") -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: "object") -> "MinioObjectStore":
        from minio import Minio

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        return cls(client)

    async def get(self, bucket: str, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)


def _parse_ref(ref: str) -> tuple[str, str] | None:
    """Split ``s3://bucket/key`` into (bucket, key); None when unparseable."""
    parts = urlsplit(ref)
    if parts.scheme != "s3" or not parts.netloc:
        return None
    key = parts.path.lstrip("/")
    if not key:
        return None
    return parts.netloc, key


async def resolve_payload(
    store: ObjectStore,
    inline: str | None,
    overflow_ref: str | None,
) -> str | None:
    """Return the full payload for a run field.

    When an overflow reference is present the full object is fetched from the
    store; otherwise the (complete) inline value is used. On any fetch error we
    fall back to the bounded inline prefix so scoring degrades gracefully
    instead of failing the whole graph.
    """
    if overflow_ref:
        parsed = _parse_ref(overflow_ref)
        if parsed is not None:
            bucket, key = parsed
            try:
                data = await store.get(bucket, key)
                return data.decode("utf-8", errors="replace")
            except Exception:
                return inline
    return inline
