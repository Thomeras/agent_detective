"""Late spans after finalization: recorded state, gated re-analysis."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone

from ingest.config import Settings
from ingest.types import graph_id_from_str
from conftest import Harness, load_fixture

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _late_batch(span_id: str) -> dict:
    """The fixture plus one extra AGENT span on the same trace (a new run)."""
    payload = copy.deepcopy(load_fixture("spawn_pipeline.json"))
    spans = [
        sp
        for rs in payload["resourceSpans"]
        for ss in rs["scopeSpans"]
        for sp in ss["spans"]
    ]
    root = next(sp for sp in spans if not sp.get("parentSpanId"))
    template = next(sp for sp in spans if sp["name"] == "scraper.retry_run")
    late_span = copy.deepcopy(template)
    late_span["spanId"] = span_id
    late_span["parentSpanId"] = root["spanId"]
    late_span["name"] = "late-agent"
    for attr in late_span["attributes"]:
        if attr["key"] == "gen_ai.agent.name":
            attr["value"] = {"stringValue": "late-agent"}
    for rs in payload["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            if root in ss["spans"]:
                ss["spans"].append(late_span)
    return payload


def test_late_spans_after_finalize_are_recorded(harness: Harness) -> None:
    graph_id = graph_id_from_str("g-spawn-1")
    asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))
    asyncio.run(harness.finalizer.scan_once(T0))
    assert harness.repo.graphs[graph_id]["status"] == "finalized"
    announced = len(harness.publisher.messages)

    asyncio.run(harness.post_traces(_late_batch("00000000000000d1")))

    graph = harness.repo.graphs[graph_id]
    assert graph["late_spans_count"] == 1
    assert graph["late_spans_last_at"] is not None
    # Default is signal-only: no re-map announcement without the flag.
    asyncio.run(harness.finalizer.scan_once(T0 + timedelta(hours=1)))
    assert len(harness.publisher.messages) == announced


def test_reanalyze_late_spans_announces_exactly_once() -> None:
    harness = Harness(Settings(reanalyze_late_spans=True))
    graph_id = graph_id_from_str("g-spawn-1")
    asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))
    asyncio.run(harness.finalizer.scan_once(T0))
    announced = len(harness.publisher.messages)

    asyncio.run(harness.post_traces(_late_batch("00000000000000d1")))

    async def _scans() -> None:
        # Late-grown graph goes through the full path once: re-map,
        # refinalize, one announcement. Further scans stay silent.
        assert len(await harness.finalizer.scan_once(T0 + timedelta(hours=1))) == 1
        assert await harness.finalizer.scan_once(T0 + timedelta(hours=2)) == []

    asyncio.run(_scans())
    assert len(harness.publisher.messages) == announced + 1
    stream, message = harness.publisher.messages[-1]
    assert message["graph_id"] == str(graph_id)
    assert message["run_count"] == 4
