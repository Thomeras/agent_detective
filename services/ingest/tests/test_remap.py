"""Finalization re-map: cross-batch structure is derived over the FULL span set.

Per-request mapping sees one OTLP batch at a time; standard BatchSpanProcessors
flush on a timer, so edges/roots/identity spans routinely arrive in different
POSTs (night_run.md, foreign pipeline cell 1). The finalizer re-maps every
stored span of the graph's trace(s) before freezing counts and announcing
completion — these tests split one known-good fixture across two POSTs and
assert the re-map heals what per-request mapping structurally cannot see.
"""

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from ingest.spans import _EPOCH, _dedupe_latest, mappable_span
from ingest.types import SpanRow, graph_id_from_str, run_id_from_key
from conftest import Harness, load_fixture

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ORCHESTRATOR_KEY = f"{TRACE_ID}:00000000000000a1"
SCRAPER_KEY = f"{TRACE_ID}:00000000000000b1"


def _split_fixture() -> tuple[dict, dict]:
    """spawn_pipeline.json as two OTLP exports, one per resource block."""
    payload = load_fixture("spawn_pipeline.json")
    first, second = payload["resourceSpans"]
    return {"resourceSpans": [first]}, {"resourceSpans": [second]}


def test_finalization_remap_derives_cross_batch_edges(harness: Harness) -> None:
    batch1, batch2 = _split_fixture()
    asyncio.run(harness.post_traces(batch1))
    asyncio.run(harness.post_traces(batch2))

    spawn_graph = graph_id_from_str("g-spawn-1")
    orchestrator_id = run_id_from_key(ORCHESTRATOR_KEY)

    # Measured per-request pathology: all three runs arrive, but the SPAWN
    # edge's endpoint spans never met in one mapping pass — no edges at all.
    assert len(harness.repo.runs) == 3
    assert harness.repo.edges == []

    asyncio.run(harness.finalizer.scan_once())

    # Re-map over the full stored span set derived the cross-batch edge.
    assert [
        (e.from_run_id, e.to_run_id)
        for e in harness.repo.edges
        if e.type == "SPAWN" and e.graph_id == spawn_graph
    ] == [(orchestrator_id, run_id_from_key(SCRAPER_KEY))]

    completed = [m for _, m in harness.publisher.messages]
    assert [m["graph_id"] for m in completed] == [str(spawn_graph)]
    assert completed[0]["run_count"] == 3
    assert harness.repo.graphs[spawn_graph]["status"] == "finalized"


def _late_header_batches() -> tuple[dict, dict]:
    """Synthetic two-batch trace: the AGENT opener ships first WITHOUT the
    correlation header; the member span carrying x-execution-graph-id trails
    in the next flush (it alone opens no run, so per-request mapping drops
    it and the run stays on the trace-fallback graph)."""

    def _attr(key: str, value: str) -> dict:
        return {"key": key, "value": {"stringValue": value}}

    trace = "dddd0000000000000000000000000001"
    opener = {
        "traceId": trace,
        "spanId": "00000000000000d1",
        "name": "solo.run",
        "startTimeUnixNano": "1751371200000000000",
        "endTimeUnixNano": "1751371260000000000",
        "attributes": [
            _attr("openinference.span.kind", "AGENT"),
            _attr("gen_ai.agent.name", "solo"),
        ],
    }
    member = {
        "traceId": trace,
        "spanId": "00000000000000d2",
        "parentSpanId": "00000000000000d1",
        "name": "solo.llm",
        "startTimeUnixNano": "1751371210000000000",
        "endTimeUnixNano": "1751371220000000000",
        "attributes": [
            _attr("openinference.span.kind", "LLM"),
            _attr("x-execution-graph-id", "g-late-1"),
        ],
    }

    def _export(span: dict) -> dict:
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", "late-svc")]},
                    "scopeSpans": [{"spans": [span]}],
                }
            ]
        }

    return _export(opener), _export(member)


def test_remap_rehomes_run_when_header_arrives_late(harness: Harness) -> None:
    batch1, batch2 = _late_header_batches()
    asyncio.run(harness.post_traces(batch1))
    asyncio.run(harness.post_traces(batch2))

    trace_graph = graph_id_from_str("dddd0000000000000000000000000001")
    late_graph = graph_id_from_str("g-late-1")
    run_id = run_id_from_key("dddd0000000000000000000000000001:00000000000000d1")

    assert set(harness.repo.graphs) == {trace_graph}
    assert harness.repo.runs[run_id].graph_id == trace_graph

    asyncio.run(harness.finalizer.scan_once())

    # The full-set re-map found the header: the run moved to the declared
    # graph and the emptied trace-fallback shell finalized SILENTLY.
    assert harness.repo.runs[run_id].graph_id == late_graph
    assert harness.repo.graphs[trace_graph]["status"] == "finalized"
    assert harness.publisher.messages == []

    # The next scan finalizes and announces the re-homed graph.
    asyncio.run(harness.finalizer.scan_once())
    completed = [m for _, m in harness.publisher.messages]
    assert [m["graph_id"] for m in completed] == [str(late_graph)]
    assert completed[0]["run_count"] == 1


def test_remap_is_a_noop_for_single_batch_graphs(harness: Harness) -> None:
    payload = load_fixture("spawn_pipeline.json")
    asyncio.run(harness.post_traces(payload))
    before_runs = dict(harness.repo.runs)
    before_edges = list(harness.repo.edges)

    asyncio.run(harness.finalizer.scan_once())

    assert harness.repo.runs == before_runs
    assert harness.repo.edges == before_edges
    assert [m["run_count"] for _, m in harness.publisher.messages] == [3]


DELEG_TRACE = "eeee0000000000000000000000000001"
DELEG_ORCH_KEY = f"{DELEG_TRACE}:00000000000000e1"
DELEG_WRITER_KEY = f"{DELEG_TRACE}:00000000000000e3"
DELEG_WRITER_SPAN = "00000000000000e3"


def _deleg_batches(target: str = "writer") -> tuple[dict, dict]:
    """One trace in two exports: the caller and its TOOL span delegating to
    ``target`` ship first; the target agent's layer arrives in a later flush."""

    def _attr(key: str, value: str) -> dict:
        return {"key": key, "value": {"stringValue": value}}

    orch = {
        "traceId": DELEG_TRACE,
        "spanId": "00000000000000e1",
        "name": "orch.run",
        "startTimeUnixNano": "1751371200000000000",
        "endTimeUnixNano": "1751371300000000000",
        "attributes": [
            _attr("openinference.span.kind", "AGENT"),
            _attr("gen_ai.agent.name", "orchestrator"),
        ],
    }
    tool = {
        "traceId": DELEG_TRACE,
        "spanId": "00000000000000e2",
        "parentSpanId": "00000000000000e1",
        "name": "orch.delegate",
        "startTimeUnixNano": "1751371210000000000",
        "endTimeUnixNano": "1751371220000000000",
        "attributes": [
            _attr("openinference.span.kind", "TOOL"),
            _attr("gen_ai.tool.target_agent", target),
        ],
    }
    writer = {
        "traceId": DELEG_TRACE,
        "spanId": DELEG_WRITER_SPAN,
        "name": "writer.run",
        "startTimeUnixNano": "1751371215000000000",
        "endTimeUnixNano": "1751371219000000000",
        "attributes": [
            _attr("openinference.span.kind", "AGENT"),
            _attr("gen_ai.agent.name", "writer"),
        ],
    }

    def _export(spans: list[dict]) -> dict:
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", "deleg-svc")]},
                    "scopeSpans": [{"spans": spans}],
                }
            ]
        }

    return _export([orch, tool]), _export([writer])


def test_finalization_resolves_delegation_split_across_posts(harness: Harness) -> None:
    batch1, batch2 = _deleg_batches()
    asyncio.run(harness.post_traces(batch1))
    asyncio.run(harness.post_traces(batch2))
    # Redelivery of both POSTs must not change the outcome.
    asyncio.run(harness.post_traces(batch1))
    asyncio.run(harness.post_traces(batch2))

    # The delegating and target layers never met in one mapping pass.
    assert harness.repo.edges == []

    asyncio.run(harness.finalizer.scan_once())

    deleg = [e for e in harness.repo.edges if e.type == "TOOL_DELEGATION"]
    assert [(e.from_run_id, e.to_run_id) for e in deleg] == [
        (run_id_from_key(DELEG_WRITER_KEY), run_id_from_key(DELEG_ORCH_KEY))
    ]


def test_finalization_resolves_delegation_against_stored_runs(harness: Harness) -> None:
    batch1, batch2 = _deleg_batches()
    asyncio.run(harness.post_traces(batch1))
    asyncio.run(harness.post_traces(batch2))
    # The target's raw spans are gone from the sink, so the full-set re-map
    # cannot see the writer run; the deferred pass closes the edge from the
    # graph's stored runs instead.
    harness.sink.rows = [r for r in harness.sink.rows if r.span_id != DELEG_WRITER_SPAN]

    asyncio.run(harness.finalizer.scan_once())

    deleg = [e for e in harness.repo.edges if e.type == "TOOL_DELEGATION"]
    assert [(e.from_run_id, e.to_run_id) for e in deleg] == [
        (run_id_from_key(DELEG_WRITER_KEY), run_id_from_key(DELEG_ORCH_KEY))
    ]
    assert "resolved at finalization" in deleg[0].detection_method


def test_unresolvable_delegation_warns_once_without_edge(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    batch1, _ = _deleg_batches(target="ghost")
    asyncio.run(harness.post_traces(batch1))

    with caplog.at_level(logging.WARNING, logger="ingest.pipeline"):
        asyncio.run(harness.finalizer.scan_once())

    # Endpoints are never invented: no edge, exactly one warning per graph.
    assert harness.repo.edges == []
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "unresolved delegations" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "ghost" in warnings[0]
    assert str(graph_id_from_str(DELEG_TRACE)) in warnings[0]


def _row(**overrides) -> SpanRow:
    base = dict(
        trace_id="t1", span_id="s1", parent_span_id="", name="agent.run",
        kind="", start_time=_EPOCH, end_time=_EPOCH, attributes="[]",
        status_code="", resource_attributes="{}",
    )
    base.update(overrides)
    return SpanRow(**base)


def test_mappable_span_undoes_storage_sentinels() -> None:
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    span = mappable_span(
        _row(
            kind="1",
            start_time=ts,
            end_time=_EPOCH,  # never finished: stored at the epoch sentinel
            status_code="2",
            attributes='[{"key": "k", "value": {"stringValue": "v"}}]',
            resource_attributes='{"service.name": "svc"}',
        )
    )
    assert span["kind"] == 1
    assert span["start_time"] == ts.isoformat()
    assert "end_time" not in span  # epoch must NOT round-trip as "ended 1970"
    assert span["status"] == {"code": 2}
    assert span["attributes"] == [{"key": "k", "value": {"stringValue": "v"}}]
    assert span["resource_attributes"] == {"service.name": "svc"}
    assert "parent_span_id" not in span


def test_dedupe_latest_keeps_one_row_per_span() -> None:
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    unfinished = _row(end_time=_EPOCH)
    finished = _row(end_time=ts)
    other = _row(span_id="s2", end_time=ts)
    deduped = _dedupe_latest([unfinished, finished, other])
    assert sorted((r.span_id, r.end_time) for r in deduped) == [
        ("s1", ts),
        ("s2", ts),
    ]
