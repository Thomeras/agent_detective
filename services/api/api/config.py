"""Runtime configuration (build spec section 7)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_detective"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "agent-detective-payloads"
    minio_secure: bool = False

    api_port: int = 8000
    web_origin: str = "http://localhost:5173"
    web_origin2: str = "http://127.0.0.1:5173"

    # Trace intake lives in the ingest service, but "the API" is where a client
    # told to use port 8000 will POST; the API forwards /v1/traces there rather
    # than owning a second span-mapping implementation.
    ingest_base_url: str = "http://ingest:8001"
    # Generous: a large OTLP batch costs ClickHouse + Postgres writes upstream.
    ingest_proxy_timeout_s: float = 60.0

    # HMAC key for evidence-ledger signature verification (env AUDIT_HMAC_KEY).
    # The default is deliberately insecure and MUST be overridden in production:
    # with the default key anyone can forge ledger signatures, so verification
    # proves nothing. Must match the worker's Settings.audit_hmac_key.
    audit_hmac_key: str = "dev-insecure-key"
