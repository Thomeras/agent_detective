"""Liveness endpoints."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    # The docker-compose healthcheck probes "/".
    return {"service": "agent-detective-api", "status": "ok"}
