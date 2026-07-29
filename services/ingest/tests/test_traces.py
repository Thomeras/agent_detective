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


def _fixture_as_protobuf(payload: dict) -> bytes:
    """Encode an OTLP/JSON fixture as a protobuf ExportTraceServiceRequest.

    Ids flip from the fixture's hex to protobuf bytes (JSON mapping: base64),
    so posting this exercises the hex re-encoding in otlp_protobuf.
    """
    import base64
    import copy

    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    payload = copy.deepcopy(payload)
    for resource_spans in payload.get("resourceSpans") or []:
        for scope_spans in resource_spans.get("scopeSpans") or []:
            for span in scope_spans.get("spans") or []:
                for key in ("traceId", "spanId", "parentSpanId"):
                    if span.get(key):
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
    message = ParseDict(payload, ExportTraceServiceRequest(), ignore_unknown_fields=True)
    return message.SerializeToString()


def test_protobuf_body_maps_identically_to_json(harness: Harness) -> None:
    """The protobuf wire format must land on the SAME graph/run/edge rows as
    OTLP/JSON — in particular the uuid5 ids, which hash the hex span ids."""
    payload = load_fixture("spawn_pipeline.json")
    json_harness = Harness()
    asyncio.run(json_harness.post_traces(payload))

    response = asyncio.run(
        harness.post_raw(
            _fixture_as_protobuf(payload), content_type="application/x-protobuf"
        )
    )

    assert response.status_code == 200
    assert response.json() == {}
    assert set(harness.repo.graphs) == set(json_harness.repo.graphs)
    assert set(harness.repo.runs) == set(json_harness.repo.runs)
    assert {(e.from_run_id, e.to_run_id, e.type) for e in harness.repo.edges} == {
        (e.from_run_id, e.to_run_id, e.type) for e in json_harness.repo.edges
    }
    # Identity/payload extraction survives the conversion, not just ids.
    for run_id, run in json_harness.repo.runs.items():
        got = harness.repo.runs[run_id]
        assert got.agent_name == run.agent_name
        assert got.input_inline == run.input_inline
        assert got.output_inline == run.output_inline


def test_corrupt_protobuf_body_rejected(harness: Harness) -> None:
    response = asyncio.run(
        harness.post_raw(b"\xff\xff\xffgarbage", content_type="application/x-protobuf")
    )
    assert response.status_code == 400


def test_non_object_json_rejected(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces([1, 2, 3]))
    assert response.status_code == 400


def test_contract_params_attribute_lands_on_the_run_row(harness: Harness) -> None:
    payload = load_fixture("spawn_pipeline.json")
    opener = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    opener["attributes"].append(
        {
            "key": "agent_detective.contract_params",
            "value": {"stringValue": '{"file_type": "pdf"}'},
        }
    )
    asyncio.run(harness.post_traces(payload))
    run = harness.repo.runs[run_id_from_key(ORCHESTRATOR_KEY)]
    assert run.contract_params == '{"file_type": "pdf"}'


def test_empty_payload_is_a_noop(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces({"resourceSpans": []}))

    assert response.status_code == 200
    assert harness.repo.graphs == {}
    assert harness.repo.runs == {}
    assert harness.sink.rows == []


def _agent_payload(trace_id: str, resource_attrs: list[dict]) -> dict:
    """Minimal ExportTraceServiceRequest: one AGENT span under one resource."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": "00000000000000a1",
                                "name": "node.run",
                                "kind": 1,
                                "startTimeUnixNano": "1752000000000000000",
                                "endTimeUnixNano": "1752000001000000000",
                                "attributes": [
                                    {
                                        "key": "openinference.span.kind",
                                        "value": {"stringValue": "AGENT"},
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_graph_type_is_populated_from_resource_service_name(harness: Harness) -> None:
    trace_id = "aaaa0000000000000000000000000001"
    payload = _agent_payload(
        trace_id,
        [{"key": "service.name", "value": {"stringValue": "generative-simon"}}],
    )

    response = asyncio.run(harness.post_traces(payload))

    assert response.status_code == 200
    graph = harness.repo.graphs[graph_id_from_str(trace_id)]
    assert graph["graph_type"] == "generative-simon"


def test_graph_type_is_none_without_resource_service_name(harness: Harness) -> None:
    trace_id = "aaaa0000000000000000000000000002"
    payload = _agent_payload(
        trace_id,
        [{"key": "telemetry.sdk.name", "value": {"stringValue": "opentelemetry"}}],
    )

    response = asyncio.run(harness.post_traces(payload))

    assert response.status_code == 200
    graph = harness.repo.graphs[graph_id_from_str(trace_id)]
    assert graph["graph_type"] is None


def test_attempt_identity_lands_on_the_run_row(harness: Harness) -> None:
    payload = load_fixture("spawn_pipeline.json")
    opener = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    opener["attributes"].extend(
        [
            {"key": "agent_detective.attempt", "value": {"stringValue": "2"}},
            {"key": "agent_detective.attempt_of", "value": {"stringValue": "builder"}},
        ]
    )
    asyncio.run(harness.post_traces(payload))
    run = harness.repo.runs[run_id_from_key(ORCHESTRATOR_KEY)]
    assert (run.attempt, run.attempt_of) == (2, "builder")
