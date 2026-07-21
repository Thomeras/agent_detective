"""Core mapping behavior: input shapes, timestamps, run fields, robustness."""

from datetime import datetime, timezone

import pytest

from otel_mapper import (
    EdgeType,
    MappingResult,
    flatten_export_request,
    map_spans,
)

NANO_TS = "1752000000123456789"
ISO_TS = "2025-07-08T18:40:00.123456Z"  # same instant as NANO_TS


def _agent_span(**overrides):
    """Minimal flat-form AGENT span."""
    span = {
        "trace_id": "t1",
        "span_id": "s1",
        "name": "agent.run",
        "start_time": NANO_TS,
        "end_time": "1752000005000000000",
        "attributes": {
            "openinference.span.kind": "AGENT",
            "gen_ai.agent.name": "orchestrator",
        },
        "status": "ok",
    }
    span.update(overrides)
    return span


def test_empty_input_returns_empty_result() -> None:
    result = map_spans([])
    assert result == MappingResult()
    assert result.runs == []
    assert result.edges == []
    assert result.graph_ids == set()


def test_non_dict_and_idless_spans_are_skipped() -> None:
    result = map_spans([None, 42, "nope", {"name": "no ids"}, {"trace_id": "t"}])  # type: ignore[list-item]
    assert result == MappingResult()


def test_run_key_is_trace_id_colon_opening_span_id() -> None:
    result = map_spans([_agent_span()])
    assert [r.run_key for r in result.runs] == ["t1:s1"]
    assert result.runs[0].trace_id == "t1"


def test_graph_id_defaults_to_trace_id_without_header() -> None:
    result = map_spans([_agent_span()])
    assert result.runs[0].graph_id == "t1"
    assert result.graph_ids == {"t1"}


def test_unix_nano_and_iso_timestamps_parse_to_same_instant() -> None:
    nano = map_spans([_agent_span()]).runs[0]
    iso = map_spans([_agent_span(start_time=ISO_TS)]).runs[0]
    expected = datetime(2025, 7, 8, 18, 40, 0, 123456, tzinfo=timezone.utc)
    assert nano.start_time == expected
    assert iso.start_time == expected
    assert nano.end_time == datetime(2025, 7, 8, 18, 40, 5, tzinfo=timezone.utc)


def test_naive_iso_timestamp_is_treated_as_utc() -> None:
    run = map_spans([_agent_span(start_time="2025-07-08T18:40:00.123456")]).runs[0]
    assert run.start_time == datetime(2025, 7, 8, 18, 40, 0, 123456, tzinfo=timezone.utc)


def test_otlp_and_flat_shapes_map_identically() -> None:
    otlp_shape = {
        "traceId": "t1",
        "spanId": "s1",
        "parentSpanId": "0000000000000000",
        "name": "agent.run",
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": NANO_TS,
        "endTimeUnixNano": "1752000005000000000",
        "attributes": [
            {"key": "openinference.span.kind", "value": {"stringValue": "AGENT"}},
            {"key": "gen_ai.agent.name", "value": {"stringValue": "orchestrator"}},
            {"key": "gen_ai.agent.version", "value": {"stringValue": "2.0.0"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "42"}},
            {"key": "gen_ai.usage.cost", "value": {"doubleValue": 0.5}},
        ],
        "status": {"code": "STATUS_CODE_OK"},
    }
    flat_shape = _agent_span(
        parent_span_id="0000000000000000",
        kind="INTERNAL",
        attributes={
            "openinference.span.kind": "AGENT",
            "gen_ai.agent.name": "orchestrator",
            "gen_ai.agent.version": "2.0.0",
            "gen_ai.usage.input_tokens": 42,
            "gen_ai.usage.cost": 0.5,
        },
    )
    assert map_spans([otlp_shape]) == map_spans([flat_shape])


def test_status_variants() -> None:
    for bad in ({"code": "STATUS_CODE_ERROR"}, {"code": 2}, "error", "STATUS_CODE_ERROR"):
        assert map_spans([_agent_span(status=bad)]).runs[0].status == "failed", bad
    for good in ({"code": "STATUS_CODE_OK"}, {"code": 1}, {"code": "STATUS_CODE_UNSET"}, "ok", None):
        assert map_spans([_agent_span(status=good)]).runs[0].status == "ok", good


def test_member_span_error_fails_the_run() -> None:
    child = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "attributes": {"openinference.span.kind": "TOOL"},
        "status": {"code": "STATUS_CODE_ERROR"},
    }
    result = map_spans([_agent_span(status="ok"), child])
    assert result.runs[0].status == "failed"


def test_agent_span_metric_wins_over_children_sum() -> None:
    child = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "attributes": {
            "openinference.span.kind": "LLM",
            "gen_ai.usage.input_tokens": 999,
        },
    }
    span = _agent_span()
    span["attributes"]["gen_ai.usage.input_tokens"] = 10
    run = map_spans([span, child]).runs[0]
    assert run.tokens_in == 10


def test_metrics_sum_over_children_when_agent_span_has_none() -> None:
    children = [
        {
            "trace_id": "t1",
            "span_id": f"s{i}",
            "parent_span_id": "s1",
            "attributes": {
                "openinference.span.kind": "LLM",
                "gen_ai.usage.input_tokens": n,
                "gen_ai.usage.output_tokens": n * 2,
            },
        }
        for i, n in ((2, 3), (3, 4))
    ]
    run = map_spans([_agent_span(), *children]).runs[0]
    assert run.tokens_in == 7
    assert run.tokens_out == 14
    assert run.cost_usd is None  # no pricing table is invented


def test_openinference_token_count_fallback_keys() -> None:
    child = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "attributes": {
            "openinference.span.kind": "LLM",
            "llm.token_count.prompt": 11,
            "llm.token_count.completion": 7,
        },
    }
    run = map_spans([_agent_span(), child]).runs[0]
    assert (run.tokens_in, run.tokens_out) == (11, 7)


def test_non_string_input_output_values_are_json_serialized() -> None:
    span = _agent_span()
    span["attributes"]["input.value"] = {"b": 1, "a": [2]}
    run = map_spans([span]).runs[0]
    assert run.input == '{"a": [2], "b": 1}'


def test_spans_without_agent_ancestor_form_no_run() -> None:
    orphan = {
        "trace_id": "t1",
        "span_id": "s9",
        "attributes": {"openinference.span.kind": "LLM", "gen_ai.usage.input_tokens": 5},
    }
    result = map_spans([orphan])
    assert result.runs == []
    assert result.edges == []


def test_flatten_export_request_attaches_resource_attributes() -> None:
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "gen_ai.agent.name", "value": {"stringValue": "res-agent"}}
                    ]
                },
                "scopeSpans": [{"spans": [{"traceId": "t1", "spanId": "s1", "attributes": []}]}],
            }
        ]
    }
    spans = flatten_export_request(payload)
    assert len(spans) == 1
    assert spans[0]["resource_attributes"] == {"gen_ai.agent.name": "res-agent"}


def test_flatten_export_request_tolerates_garbage() -> None:
    assert flatten_export_request({}) == []
    assert flatten_export_request({"resourceSpans": [None, {"scopeSpans": None}]}) == []
    assert flatten_export_request("not a dict") == []  # type: ignore[arg-type]


def test_agent_name_falls_back_to_resource_attributes() -> None:
    span = _agent_span()
    del span["attributes"]["gen_ai.agent.name"]
    span["resource_attributes"] = {"gen_ai.agent.name": "res-agent", "gen_ai.agent.version": "3.1"}
    run = map_spans([span]).runs[0]
    assert run.agent_name == "res-agent"
    assert run.agent_version == "3.1"


def test_edge_type_enum_matches_database_values() -> None:
    assert {e.value for e in EdgeType} == {"SPAWN", "A2A_MESSAGE", "TOOL_DELEGATION"}
    assert EdgeType.SPAWN == "SPAWN"


def test_output_is_deterministic_for_same_input() -> None:
    spans = [
        _agent_span(),
        _agent_span(span_id="s2", parent_span_id="s1",
                    attributes={"openinference.span.kind": "AGENT",
                                "gen_ai.agent.name": "worker"}),
    ]
    assert map_spans(spans) == map_spans(list(reversed(spans)))
