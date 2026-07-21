"""Synthetic pipeline configuration (build spec sections 6.5 and 7).

Every value comes from an environment variable so the demo runs unchanged
against docker-compose or a local checkout. Defaults target a local setup:
ingest on 8001, mock LLM on 8080.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(extra="ignore")

    # Where OTLP/HTTP JSON traces are POSTed. Empty string => dry run (no POST).
    ingest_url: str = "http://localhost:8001"

    # OpenAI-compatible chat-completions base URL (the mock LLM by default).
    llm_base_url: str = "http://localhost:8080/v1"
    llm_model: str = "mock-llm"
    llm_timeout_s: float = 15.0

    # The flagship fault switch: when true the scraper fabricates prices that
    # the source pages never listed, and downstream agents process them
    # faithfully (silent hallucination).
    scraper_hallucinate: bool = False

    # Correlation id joining all five runs into one execution graph. Empty =>
    # a fresh random id per run.
    graph_id: str = ""

    # Deterministic ids and timestamps (used to regenerate fixture testdata).
    deterministic: bool = False

    # When set, the OTLP payload is written to this path instead of POSTed.
    capture_file: str = ""
