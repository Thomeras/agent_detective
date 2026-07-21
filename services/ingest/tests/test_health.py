"""GET /health: aggregate connectivity status for Postgres/ClickHouse/Redis."""

import asyncio

from conftest import Harness


def test_health_ok(harness: Harness) -> None:
    response = asyncio.run(harness.health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "postgres": True,
        "clickhouse": True,
        "redis": True,
    }


def test_health_degraded_when_a_dependency_is_down(harness: Harness) -> None:
    harness.repo.fail_ping = True
    harness.sink.fail_ping = True

    response = asyncio.run(harness.health())

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "postgres": False,
        "clickhouse": False,
        "redis": True,
    }
