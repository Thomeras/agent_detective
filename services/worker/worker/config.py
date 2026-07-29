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

    @field_validator("cost_budget_default_usd", "judge_seed", mode="before")
    @classmethod
    def _empty_str_is_none(cls, value: object) -> object:
        # COST_BUDGET_DEFAULT_USD / JUDGE_SEED are intentionally unset in
        # compose/.env, which arrives as an empty string; treat that as None
        # ("no budget" / "no seed").
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
    # Determinism knob: sent as "seed" in /chat/completions when set. Best
    # effort — temperature=0 + seed still does not guarantee bitwise-identical
    # completions on most backends; measure with scripts/determinism_probe.py.
    judge_seed: int | None = None
    # Deterministic-first gate. With it on, tier2 runs its deterministic half
    # over every node FIRST — which costs no model calls at all — and skips the
    # per-node judged pass when that half already localised a defect it observed
    # the origin of. The saving is the whole per-node fan-out, N calls down to
    # zero, on exactly the graphs where the judge had nothing left to decide.
    # OFF by default: it trades the judged score column (and any SECOND,
    # independent origin only the judge would have found) for cost, and that is
    # an operator's call, not a default.
    judge_gate: bool = False

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

    # Artifact integrity (docs/deterministic-signals.md A1): a declared artifact
    # smaller than this is treated as empty/truncated -> artifact_integrity_fail.
    min_artifact_bytes: int = 64

    # Blame engine knobs (mirrors BlameConfig defaults).
    max_loop_iterations: int = 10
    blame_threshold: float = 0.5
    gap_threshold: float = 0.25
    min_drop: float = 0.10
    # Cumulative degradation: total decline over >= cum_min_edges consecutive
    # dropping edges that counts as an origin even without a single big drop.
    cum_drop_threshold: float = 0.30
    cum_min_edges: int = 2
    cum_step_min: float = 0.05

    # Alerting.
    slack_webhook_url: str | None = None
    ui_base_url: str = "http://localhost:5173"

    # Evidence-ledger HMAC key (roadmap 2.6). The default is intentionally
    # loud about being insecure: it MUST be overridden in production
    # (AUDIT_HMAC_KEY env var), otherwise the ledger's signatures prove
    # nothing — anyone with the default key can forge them.
    audit_hmac_key: str = "dev-insecure-key"

    # Circuit breaker (roadmap 2.3): number of open incidents blamed on one
    # agent that RECORDS an open breaker decision. Recording only — Agent
    # Detective observes; enforcement happens only if the integration polls.
    breaker_open_incidents: int = 3

    # Stream consumer operations.
    consumer_name: str = "worker-1"
    stream_block_ms: int = 5000
    stream_batch_size: int = 16
    max_deliveries: int = 5
    reaper_idle_ms: int = 60000

    @property
    def payload_inline_max_bytes(self) -> int:
        return self.payload_inline_max_kb * 1024
