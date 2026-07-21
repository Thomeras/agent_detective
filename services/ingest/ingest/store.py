"""Payload object storage: the inline/overflow routing rule plus the MinIO
implementation behind a thin async protocol (build spec sections 5 and 6.2).

Overflow payloads go to bucket ``agent-detective-payloads`` under
``payloads/{graph_id}/{run_id}/{input|output}``; the ``*_overflow_ref`` column
stores ``s3://{bucket}/{key}``. Even on overflow the inline column keeps a
bounded prefix of the payload (spec section 5 note), and a short derived
summary is always produced for the UI.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Protocol
from uuid import UUID

from .types import SUMMARY_CHARS, StoredPayload


class ObjectStore(Protocol):
    """Minimal async object-storage seam; faked in tests."""

    async def put(self, bucket: str, key: str, data: bytes) -> str:
        """Store ``data`` and return the reference persisted in the DB."""
        ...

    async def ensure_bucket(self, bucket: str) -> None:
        """Create the bucket if missing (best-effort at service startup)."""
        ...


class MinioObjectStore:
    """MinIO-backed store. The minio client is synchronous, so calls are
    dispatched to a worker thread to keep the event loop free."""

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

    async def put(self, bucket: str, key: str, data: bytes) -> str:
        def _put() -> None:
            self._client.put_object(bucket, key, BytesIO(data), length=len(data))

        await asyncio.to_thread(_put)
        return f"s3://{bucket}/{key}"

    async def ensure_bucket(self, bucket: str) -> None:
        def _ensure() -> None:
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)

        await asyncio.to_thread(_ensure)


async def store_payload(
    store: ObjectStore,
    bucket: str,
    graph_id: UUID,
    run_id: UUID,
    kind: str,
    value: str | None,
    inline_max_bytes: int,
) -> StoredPayload:
    """Route one payload inline or to the object store.

    ``kind`` is ``"input"`` or ``"output"``. At or below the inline limit the
    full value stays inline; above it the full payload is stored under
    ``payloads/{graph_id}/{run_id}/{kind}`` and the inline column keeps a
    bounded prefix (truncated on a UTF-8 character boundary).
    """
    if value is None:
        return StoredPayload(inline=None, overflow_ref=None, nbytes=0, summary=None)
    data = value.encode("utf-8")
    nbytes = len(data)
    summary = value[:SUMMARY_CHARS]
    if nbytes <= inline_max_bytes:
        return StoredPayload(inline=value, overflow_ref=None, nbytes=nbytes, summary=summary)
    key = f"payloads/{graph_id}/{run_id}/{kind}"
    ref = await store.put(bucket, key, data)
    inline = data[:inline_max_bytes].decode("utf-8", errors="ignore")
    return StoredPayload(inline=inline, overflow_ref=ref, nbytes=nbytes, summary=summary)
