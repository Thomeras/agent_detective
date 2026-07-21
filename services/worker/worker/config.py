"""Worker service configuration (build spec sections 4 and 7).

All values come from environment variables (upper-cased field names); defaults
match .env.example so a local checkout works against the docker-compose
infrastructure as-is. Client constructors elsewhere are lazy, so importing a
module that holds a Settings instance never touches the network.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(extra="ignore")

    @field_validator("cost_budget_default_usd", mode="before")
    @classmethod
    def _empty_str_is_none(cls, value: object) -> object:
        # COST_BUDGET_DEFAULT_USD is intentionally unset in compose/.env, which
        # arrives as an empty string; treat that as "no budget" (None).
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_detective"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "agent-detective-payloads"
    minio_secure: bool = False

    # Judge (OpenAI-compatible chat/completions endpoint).
    judge_base_url: str = "http://localhost:8080/v1"
    judge_api_key: str = "sk-none"
    judge_model: str = "judge"
    judge_timeout_s: float = 30.0
    judge_concurrency: int = 4
    judge_max_tokens: int = 1024

    # Scoring weights and the renormalization floor (spec 4.3 step 2).
    score_w_schema: float = 0.35
    score_w_judge: float = 0.40
    score_w_heuristics: float = 0.15
    score_min_weight: float = 0.5

    # Payloads at or below this size stayed inline in Postgres; larger ones are
    # fetched from the object store.
    payload_inline_max_kb: int = 64

    # Percentage of unflagged graphs sampled into tier2 (default 0, demo 100).
    tier2_sample_pct: int = 0

    # Cost budget per graph_type; None disables the cost_overrun flag.
    cost_budget_default_usd: float | None = None

    # Blame engine knobs (mirrors BlameConfig defaults).
    max_loop_iterations: int = 10
    blame_threshold: float = 0.5
    gap_threshold: float = 0.25
    min_drop: float = 0.10

    # Alerting.
    slack_webhook_url: str | None = None
    ui_base_url: str = "http://localhost:5173"

    # Stream consumer operations.
    consumer_name: str = "worker-1"
    stream_block_ms: int = 5000
    stream_batch_size: int = 16
    max_deliveries: int = 5
    reaper_idle_ms: int = 60000

    @property
    def payload_inline_max_bytes(self) -> int:
        return self.payload_inline_max_kb * 1024
