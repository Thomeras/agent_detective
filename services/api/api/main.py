"""Read API for graphs, incidents, blame reports and agent stats (build spec section 6.3)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings
from .db import create_engine, create_session_factory
from .payloads import MinioPayloadStore
from .repository import SqlRepository
from .routers import (
    agents,
    audit,
    calibration,
    contracts,
    control,
    graphs,
    health,
    incidents,
    traces,
)
from .routers.traces import IngestProxy
from .streams import RedisStreamPublisher


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.repository = SqlRepository(create_session_factory(engine))
        app.state.payload_store = MinioPayloadStore(settings)
        publisher = RedisStreamPublisher(settings.redis_url)
        app.state.publisher = publisher
        proxy = IngestProxy(settings)
        app.state.ingest_proxy = proxy
        yield
        await proxy.close()
        await publisher.close()
        await engine.dispose()

    app = FastAPI(title="Agent Detective API", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin, settings.web_origin2],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(graphs.router)
    app.include_router(incidents.router)
    app.include_router(agents.router)
    app.include_router(calibration.router)
    app.include_router(control.router)
    app.include_router(contracts.router)
    app.include_router(audit.router)
    app.include_router(traces.router)
    return app


app = create_app()
