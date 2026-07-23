"""POST /v1/traces: happy path, idempotent redelivery, edge cases."""

import asyncio

from ingest.types import graph_id_from_str, run_id_from_key
from conftest import Harness, load_fixture

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ORCHESTRATOR_KEY = f"{TRACE_ID}:00000000000000a1"
SCRAPER_KEY = f"{TRACE_ID}:00000000000000b1"
SCRAPER_RETRY_KEY = f"{TRACE_ID}:00000000000000b3"


def test_post_spawn_pipeline_writes_expected_rows(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))

    assert response.status_code == 200
    assert response.json() == {}

    # Raw spans captured for ClickHouse, one per span in the fixture.
    assert len(harness.sink.rows) == 7
    assert {r.trace_id for r in harness.sink.rows} == {TRACE_ID}
    assert harness.sink.rows[0].span_id == "00000000000000a1"
    assert "openinference.span.kind" in harness.sink.rows[0].attributes

    # One execution graph with three runs and one SPAWN edge.
    graph_id = graph_id_from_str("g-spawn-1")
    assert set(harness.repo.graphs) == {graph_id}
    graph = harness.repo.graphs[graph_id]
    assert graph["status"] == "active"
    assert graph["run_count"] == 3
    assert graph["started_at"] is not None and graph["ended_at"] is not None

    assert len(harness.repo.runs) == 3
    orchestrator = harness.repo.runs[run_id_from_key(ORCHESTRATOR_KEY)]
    assert orchestrator.graph_id == graph_id
    assert orchestrator.agent_name == "orchestrator"
    assert orchestrator.agent_version == "1.4.0"
    assert orchestrator.model_name == "gpt-4o-mini"  # from the member LLM span
    assert orchestrator.prompt_hash == "ab12cd34ef56"
    assert orchestrator.tool_schema_hash == "0011aabbccdd"
    # Out-of-band artifact integrity metadata: the raw opener-span attribute
    # string lands on the run row (the worker reads it from here, never from
    # forgeable payload text).
    assert orchestrator.artifact_meta == (
        '[{"path":"out/products.json","size":2048,"sha256":"deadbeefcafe",'
        '"declared_ext":"json","detected_kind":"json","parse_ok":true,"nonempty":true}]'
    )
    scraper = harness.repo.runs[run_id_from_key(SCRAPER_KEY)]
    assert scraper.artifact_meta is None  # absent on the wire -> NULL, never invented
    assert scraper.tool_schema_hash is None  # same rule: absent -> NULL
    # Mapper-derived TOOL-span digest lands on the run row; runs without TOOL
    # member spans stay NULL, never an empty array.
    assert orchestrator.tool_calls is None
    retry = harness.repo.runs[run_id_from_key(SCRAPER_RETRY_KEY)]
    assert retry.tool_calls == (
        '[{"name":"fetch_page","args_sha":"5bba4ef7e89c","status":"ok"},'
        '{"name":"scraper.parse_html","args_sha":"e3b0c44298fc","status":"error"}]'
    )
    assert orchestrator.trace_id == TRACE_ID
    assert orchestrator.status == "ok"
    assert orchestrator.cost_usd == 0.012
    assert orchestrator.tokens_in == 1200
    assert orchestrator.tokens_out == 300
    # Small payloads stay inline; summaries are derived for the UI.
    assert orchestrator.input_inline == "Find three products and translate them."
    assert orchestrator.output_inline == "Published 3 localized products."
    assert orchestrator.input_overflow_ref is None
    assert orchestrator.output_overflow_ref is None
    assert orchestrator.input_bytes == len(orchestrator.input_inline.encode("utf-8"))
    assert orchestrator.input_summary == orchestrator.input_inline
    assert orchestrator.output_summary == orchestrator.output_inline
    assert orchestrator.started_at is not None and orchestrator.ended_at is not None

    assert len(harness.repo.edges) == 1
    edge = harness.repo.edges[0]
    assert edge.graph_id == graph_id
    assert (edge.from_run_id, edge.to_run_id) == (
        run_id_from_key(ORCHESTRATOR_KEY),
        run_id_from_key(SCRAPER_KEY),
    )
    assert edge.type == "SPAWN"
    assert edge.detection_method.startswith("rule=spawn")


def test_redelivery_is_a_noop(harness: Harness) -> None:
    payload = load_fixture("spawn_pipeline.json")

    async def _post_twice() -> None:
        first = await harness.post_traces(payload)
        second = await harness.post_traces(payload)
        assert first.status_code == 200
        assert second.status_code == 200

    asyncio.run(_post_twice())

    graph_id = graph_id_from_str("g-spawn-1")
    graph = harness.repo.graphs[graph_id]
    # run_count must not double-bump; rows are unchanged.
    assert graph["run_count"] == 3
    assert len(harness.repo.graphs) == 1
    assert len(harness.repo.runs) == 3
    assert len(harness.repo.edges) == 1
    # Raw span storage is append-only; duplicates there are acceptable.
    assert len(harness.sink.rows) == 14


def test_malformed_payload_is_accepted_gracefully(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces(load_fixture("malformed.json")))

    assert response.status_code == 200
    # The two usable AGENT spans open runs; junk spans are skipped.
    assert len(harness.repo.runs) == 2
    assert len(harness.repo.edges) == 1
    # No correlation header in the fixture: graph id falls back to trace id.
    assert set(harness.repo.graphs) == {
        graph_id_from_str("eeee0000000000000000000000000005")
    }


def test_correlation_header_groups_traces_into_one_graph(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces(load_fixture("correlation_header.json")))

    assert response.status_code == 200
    assert set(harness.repo.graphs) == {graph_id_from_str("g-corr-1")}
    assert len(harness.repo.runs) == 2
    # The header gives membership only; no structural edges exist.
    assert harness.repo.edges == []


def test_non_json_body_rejected(harness: Harness) -> None:
    response = asyncio.run(harness.post_raw(b"this is not json"))
    assert response.status_code == 400


def test_non_object_json_rejected(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces([1, 2, 3]))
    assert response.status_code == 400


def test_empty_payload_is_a_noop(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces({"resourceSpans": []}))

    assert response.status_code == 200
    assert harness.repo.graphs == {}
    assert harness.repo.runs == {}
    assert harness.sink.rows == []
