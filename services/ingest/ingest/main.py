"""Ingest service entrypoint (build spec section 6.2).

FastAPI app with:

- ``POST /v1/traces`` — OTLP/HTTP (ExportTraceServiceRequest), JSON or
  protobuf by content-type. Raw spans
  go to ClickHouse ``otel_spans``, then otel_mapper candidates are upserted
  into Postgres (graphs/runs/edges, idempotent under redelivery), and
  input/output payloads are routed inline or to MinIO by size.
- ``GET /health`` — Postgres + ClickHouse + Redis connectivity.
- a background finalizer task that flips quiesced / root-ended graphs to
  ``finalized`` and announces them on the ``ad.graphs.completed`` stream.

All external systems sit behind thin async protocols (Repo, SpanSink,
StreamPublisher, ObjectStore) injected into ``create_app``; production builds
the real clients in ``build_dependencies``, tests inject in-memory fakes.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .finalizer import Finalizer
from .otlp_protobuf import parse_protobuf_traces
from .pipeline import TraceRemapper, build_batch
from .repository import PgRepo, Repo
from .spans import ClickHouseSpanSink, SpanSink
from .store import MinioObjectStore, ObjectStore
from .stream import RedisStreamPublisher, StreamPublisher

logger = logging.getLogger(__name__)


@dataclass
class Dependencies:
    """Everything the app needs from the outside world; one bag, all seams."""

    repo: Repo
    span_sink: SpanSink
    publisher: StreamPublisher
    store: ObjectStore


def build_dependencies(settings: Settings) -> Dependencies:
    """Construct the real client-backed implementations from settings.

    Client constructors are lazy (no network I/O), so importing this module
    and building the app is safe without live infrastructure.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    return Dependencies(
        repo=PgRepo(create_async_engine(settings.database_url)),
        span_sink=ClickHouseSpanSink.from_url(settings.clickhouse_url),
        publisher=RedisStreamPublisher.from_url(settings.redis_url),
        store=MinioObjectStore.from_settings(settings),
    )


def create_app(settings: Settings, deps: Dependencies) -> FastAPI:
    finalizer = Finalizer(
        deps.repo,
        deps.publisher,
        settings.graph_quiescence_seconds,
        remapper=TraceRemapper(deps.repo, deps.span_sink, settings, deps.store),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await deps.store.ensure_bucket(settings.minio_bucket)
        except Exception:
            # Compose's createbuckets also creates it; a missing bucket only
            # fails when an overflow payload actually arrives.
            logger.exception("could not ensure payload bucket; continuing")
        task = asyncio.create_task(finalizer.run_forever(settings.finalizer_check_seconds))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(
                deps.repo.close(), deps.span_sink.close(), deps.publisher.close(),
                return_exceptions=True,
            )

    app = FastAPI(title="agent-detective-ingest", lifespan=lifespan)
    app.state.settings = settings
    app.state.deps = deps
    app.state.finalizer = finalizer

    @app.post("/v1/traces")
    async def post_traces(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        if "protobuf" in content_type:
            try:
                payload: Any = parse_protobuf_traces(await request.body())
            except Exception:
                return JSONResponse(
                    {"detail": "request body must be a valid OTLP ExportTraceServiceRequest protobuf"},
                    status_code=400,
                )
        else:
            try:
                payload = await request.json()
            except Exception:
                return JSONResponse(
                    {
                        "detail": "request body must be JSON"
                        " (or OTLP protobuf with content-type application/x-protobuf)"
                    },
                    status_code=400,
                )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"detail": "request body must be an OTLP ExportTraceServiceRequest JSON object"},
                status_code=400,
            )
        span_rows, batch = await build_batch(payload, settings, deps.store)
        await deps.span_sink.insert_spans(span_rows)
        await deps.repo.upsert_batch(batch)
        finalizer.touch(batch.graph_ids)
        # An empty object is a valid OTLP ExportTraceServiceResponse.
        return JSONResponse({})

    @app.get("/health")
    async def health() -> JSONResponse:
        checks: dict[str, bool] = {}
        for name, ping in (
            ("postgres", deps.repo.ping),
            ("clickhouse", deps.span_sink.ping),
            ("redis", deps.publisher.ping),
        ):
            try:
                await ping()
                checks[name] = True
            except Exception:
                logger.warning("health check failed: %s", name, exc_info=True)
                checks[name] = False
        ok = all(checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", **checks},
            status_code=200 if ok else 503,
        )

    return app


# Module-level app for uvicorn. Settings come from the environment; client
# construction is lazy, so importing this has no side effects on the network.
_settings = Settings()
app = create_app(_settings, build_dependencies(_settings))
