"""GET /config: the effective runtime settings, visible without docker exec.

The endpoint exists so a multi-stage pipeline can learn the quiescence window
BEFORE it runs into it. Two rules keep it safe to expose: it is read-only, and
it whitelists non-secret fields — credentials and connection strings never
leave the process.
"""

import asyncio

from conftest import Harness
from ingest.config import Settings


def test_config_reports_the_effective_values() -> None:
    settings = Settings(
        graph_quiescence_seconds=120.0,
        finalizer_check_seconds=9.0,
        a2a_detection=True,
        reanalyze_late_spans=True,
        payload_inline_max_kb=128,
    )
    response = asyncio.run(Harness(settings).config())

    assert response.status_code == 200
    assert response.json() == {
        "graph_quiescence_seconds": 120.0,
        "finalizer_check_seconds": 9.0,
        "a2a_detection": True,
        "reanalyze_late_spans": True,
        "payload_inline_max_kb": 128,
    }


def test_config_never_exposes_credentials_or_connection_strings(
    harness: Harness,
) -> None:
    body = asyncio.run(harness.config()).json()

    for key in (
        "database_url",
        "clickhouse_url",
        "redis_url",
        "minio_endpoint",
        "minio_access_key",
        "minio_secret_key",
    ):
        assert key not in body


def test_config_is_read_only(harness: Harness) -> None:
    response = asyncio.run(harness.config(method="POST"))

    assert response.status_code == 405
