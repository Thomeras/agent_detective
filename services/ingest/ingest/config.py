"""Ingest service configuration (build spec sections 6.2 and 7).

All values come from environment variables; defaults match .env.example so a
local checkout works against the docker-compose infrastructure as-is.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_detective"
    clickhouse_url: str = "http://localhost:8123"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "agent-detective-payloads"
    minio_secure: bool = False

    # Feature flag: A2A_MESSAGE edge detection (build spec 6.1, default off).
    a2a_detection: bool = False

    # Finalizer: a graph with no new spans for this long is finalized.
    graph_quiescence_seconds: float = 30.0
    # How often the background finalizer task scans active graphs.
    finalizer_check_seconds: float = 5.0

    # Payloads at or below this size stay inline in Postgres; larger ones go
    # to the object store with only a bounded prefix kept inline.
    payload_inline_max_kb: int = 64

    # Service port (used by the Dockerfile CMD; kept here for symmetry with
    # the spec's env list).
    ingest_port: int = 8001

    @property
    def payload_inline_max_bytes(self) -> int:
        return self.payload_inline_max_kb * 1024
