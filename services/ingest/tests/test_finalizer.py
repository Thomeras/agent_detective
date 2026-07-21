"""Finalizer: quiescence, root-ended, one-shot stream announce, DB fallback."""

import asyncio
from datetime import datetime, timedelta, timezone

from ingest.finalizer import Finalizer
from ingest.types import STREAM_GRAPHS_COMPLETED, graph_id_from_str
from conftest import Harness, load_fixture

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _open_root_payload() -> dict:
    """One AGENT span that has not ended (no endTimeUnixNano)."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "gen_ai.agent.name", "value": {"stringValue": "orchestrator"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "ffff0000000000000000000000000001",
                                "spanId": "0000000000000f01",
                                "name": "orchestrator.run",
                                "kind": 1,
                                "startTimeUnixNano": "1752000000000000000",
                                "attributes": [
                                    {
                                        "key": "openinference.span.kind",
                                        "value": {"stringValue": "AGENT"},
                                    }
                                ],
                                "status": {"code": "STATUS_CODE_UNSET"},
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_root_ended_graph_finalizes_immediately(harness: Harness) -> None:
    asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))

    before = datetime.now(timezone.utc)
    finalized = asyncio.run(harness.finalizer.scan_once())

    graph_id = graph_id_from_str("g-spawn-1")
    assert [r.graph_id for r in finalized] == [graph_id]
    graph = harness.repo.graphs[graph_id]
    assert graph["status"] == "finalized"
    assert graph["finalized_at"] is not None
    assert before <= graph["finalized_at"] <= datetime.now(timezone.utc)
    assert graph["run_count"] == 3
    assert abs(graph["total_cost_usd"] - 0.016) < 1e-9

    # Exactly one message on ad.graphs.completed, exact spec 4.1 shape.
    assert len(harness.publisher.messages) == 1
    stream, message = harness.publisher.messages[0]
    assert stream == STREAM_GRAPHS_COMPLETED
    assert stream == "ad.graphs.completed"
    assert message == {
        "schema_version": 1,
        "graph_id": str(graph_id),
        "finalized_at": graph["finalized_at"].isoformat(),
        "run_count": 3,
    }


def test_quiescence_finalizes_after_idle_period(harness: Harness) -> None:
    asyncio.run(harness.post_traces(_open_root_payload()))
    # Fresh finalizer with a controllable clock; the root run has no end
    # timestamp, so only quiescence can finalize the graph.
    finalizer = Finalizer(harness.repo, harness.publisher, 30.0, clock=lambda: T0)
    graph_id = graph_id_from_str("ffff0000000000000000000000000001")
    finalizer.touch({graph_id})

    async def _scans() -> None:
        assert await finalizer.scan_once(T0) == []
        assert await finalizer.scan_once(T0 + timedelta(seconds=29)) == []
        result = await finalizer.scan_once(T0 + timedelta(seconds=30))
        assert [r.graph_id for r in result] == [graph_id]

    asyncio.run(_scans())

    graph = harness.repo.graphs[graph_id]
    assert graph["status"] == "finalized"
    assert graph["finalized_at"] == T0 + timedelta(seconds=30)
    assert harness.publisher.messages == [
        (
            STREAM_GRAPHS_COMPLETED,
            {
                "schema_version": 1,
                "graph_id": str(graph_id),
                "finalized_at": (T0 + timedelta(seconds=30)).isoformat(),
                "run_count": 1,
            },
        )
    ]


def test_finalize_is_one_shot(harness: Harness) -> None:
    asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))

    async def _scans() -> None:
        first = await harness.finalizer.scan_once()
        assert len(first) == 1
        # A second scan must not re-announce: the repo's status guard makes
        # the repeat finalize a no-op and no message is published.
        assert await harness.finalizer.scan_once() == []

    asyncio.run(_scans())
    assert len(harness.publisher.messages) == 1


def test_restart_fallback_uses_db_activity(harness: Harness) -> None:
    asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))
    # A fresh finalizer (as after a process restart) has no in-memory
    # last-seen; it must fall back to the DB activity timestamps, which are
    # far in the past here, so the graph ages out immediately.
    finalizer = Finalizer(harness.repo, harness.publisher, 30.0)

    finalized = asyncio.run(finalizer.scan_once(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    graph_id = graph_id_from_str("g-spawn-1")
    assert [r.graph_id for r in finalized] == [graph_id]
    assert len(harness.publisher.messages) == 1
