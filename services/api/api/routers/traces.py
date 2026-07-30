"""OTLP trace intake on the API port (P7).

Ingest owns span mapping and listens on 8001, but a client told "the API is on
8000" posts traces to 8000 — and used to get a bare 404 with nothing to explain
it. This router forwards ``POST /v1/traces`` to ``INGEST_BASE_URL`` verbatim:
the body streams through, and ingest's own status and body come back unchanged,
so there is still exactly one intake implementation and the caller sees the real
answer (including ingest's 400s) rather than a proxy's opinion of it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["traces"])

# Only what ingest actually reads; hop-by-hop and length headers are httpx's job.
_FORWARDED_HEADERS = ("content-type", "content-encoding", "accept")


class IngestProxy:
    """Forwards trace POSTs to the ingest service. The httpx client is built on
    first use so importing/creating the app never opens a connection."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ingest_base_url.rstrip("/")
        self._timeout = settings.ingest_proxy_timeout_s
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._client

    async def forward(self, request: Request, path: str) -> Response:
        import httpx

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in _FORWARDED_HEADERS
        }
        try:
            # Stream the body: an OTLP batch can be large and buffering it here
            # would only duplicate what ingest is about to read anyway.
            upstream = await self._http().post(path, content=request.stream(), headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("trace forward to %s%s failed: %s", self._base_url, path, exc)
            return JSONResponse(
                {
                    "detail": (
                        f"trace intake could not reach the ingest service at {self._base_url}"
                        f" ({exc.__class__.__name__}). The API forwards POST {path} to ingest;"
                        " check that the ingest service is running and INGEST_BASE_URL is correct."
                    )
                },
                status_code=502,
            )
        # Pass ingest's answer back untouched — its 400s are the honest ones.
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def get_ingest_proxy(request: Request) -> IngestProxy:
    """Proxy from app.state; built on demand for apps that skipped the lifespan."""
    proxy = getattr(request.app.state, "ingest_proxy", None)
    if proxy is None:
        proxy = IngestProxy(request.app.state.settings)
        request.app.state.ingest_proxy = proxy
    return proxy


@router.post("/v1/traces")
async def post_traces(request: Request) -> Response:
    return await get_ingest_proxy(request).forward(request, "/v1/traces")
