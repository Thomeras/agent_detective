"""Payload object store: MinIO holds inputs/outputs that did not fit inline.

Overflow object keys are stored on the run row (`input_overflow_ref` /
`output_overflow_ref`); the bucket comes from settings. The minio SDK is
synchronous, so calls are pushed to a thread.
"""

import asyncio

from minio import Minio

from .config import Settings


class MinioPayloadStore:
    def __init__(self, settings: Settings):
        self._bucket = settings.minio_bucket
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    async def get_text(self, object_key: str) -> str:
        return await asyncio.to_thread(self._get_text_sync, object_key)

    def _get_text_sync(self, object_key: str) -> str:
        response = self._client.get_object(self._bucket, object_key)
        try:
            return response.read().decode("utf-8")
        finally:
            response.close()
            response.release_conn()
