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


def test_model_and_prompt_hash_span_attrs_win_over_resource() -> None:
    span = _agent_span()
    span["attributes"]["gen_ai.request.model"] = "span-model"
    span["attributes"]["agent_detective.prompt_hash"] = "aaaa00000001"
    span["attributes"]["agent_detective.tool_schema_hash"] = "cccc00000003"
    span["resource_attributes"] = {
        "gen_ai.request.model": "res-model",
        "agent_detective.prompt_hash": "bbbb00000002",
        "agent_detective.tool_schema_hash": "dddd00000004",
    }
    run = map_spans([span]).runs[0]
    assert run.model_name == "span-model"
    assert run.prompt_hash == "aaaa00000001"
    assert run.tool_schema_hash == "cccc00000003"


def test_model_and_prompt_hash_fall_back_to_resource_attributes() -> None:
    span = _agent_span()
    span["resource_attributes"] = {
        "gen_ai.request.model": "res-model",
        "agent_detective.prompt_hash": "bbbb00000002",
        "agent_detective.tool_schema_hash": "dddd00000004",
    }
    run = map_spans([span]).runs[0]
    assert run.model_name == "res-model"
    assert run.prompt_hash == "bbbb00000002"
    assert run.tool_schema_hash == "dddd00000004"


def test_model_and_prompt_hash_are_none_when_absent() -> None:
    run = map_spans([_agent_span()]).runs[0]
    assert run.model_name is None
    assert run.prompt_hash is None
    assert run.tool_schema_hash is None


def test_model_falls_back_to_first_member_llm_span_in_execution_order() -> None:
    # Standard GenAI semconv emits gen_ai.request.model on child LLM spans,
    # not the AGENT span. The earliest-starting member carrying it wins.
    later = {
        "trace_id": "t1",
        "span_id": "s3",
        "parent_span_id": "s1",
        "start_time": "1752000003000000000",
        "attributes": {"openinference.span.kind": "LLM", "gen_ai.request.model": "late-model"},
    }
    earlier = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "start_time": "1752000001000000000",
        "attributes": {"openinference.span.kind": "LLM", "gen_ai.request.model": "early-model"},
    }
    # Input order is later-first: execution order (start time) must win.
    run = map_spans([_agent_span(), later, earlier]).runs[0]
    assert run.model_name == "early-model"


def test_opener_and_resource_model_win_over_member_llm_span() -> None:
    child = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "attributes": {"openinference.span.kind": "LLM", "gen_ai.request.model": "child-model"},
    }
    opener = _agent_span()
    opener["attributes"]["gen_ai.request.model"] = "opener-model"
    assert map_spans([opener, child]).runs[0].model_name == "opener-model"

    via_resource = _agent_span(resource_attributes={"gen_ai.request.model": "res-model"})
    assert map_spans([via_resource, child]).runs[0].model_name == "res-model"


ARTIFACT_META = '[{"path":"out/report.md","size":10,"sha256":"aa","parse_ok":true}]'


def test_artifact_meta_extracted_verbatim_from_opener_span() -> None:
    span = _agent_span()
    span["attributes"]["agent_detective.artifact_meta"] = ARTIFACT_META
    run = map_spans([span]).runs[0]
    assert run.artifact_meta == ARTIFACT_META


def test_artifact_meta_absent_is_none() -> None:
    assert map_spans([_agent_span()]).runs[0].artifact_meta is None


def test_artifact_meta_has_no_resource_fallback() -> None:
    # artifact_meta is per-run data: a resource-level value would smear one
    # node's artifact onto every run exported under that resource.
    span = _agent_span(resource_attributes={"agent_detective.artifact_meta": ARTIFACT_META})
    assert map_spans([span]).runs[0].artifact_meta is None


def test_artifact_meta_on_member_span_does_not_leak_to_the_run() -> None:
    child = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "attributes": {
            "openinference.span.kind": "TOOL",
            "agent_detective.artifact_meta": ARTIFACT_META,
        },
    }
    assert map_spans([_agent_span(), child]).runs[0].artifact_meta is None


CONTRACT_PARAMS = '{"file_type": "pdf", "lang": "cs"}'


def test_contract_params_extracted_verbatim_from_opener_span() -> None:
    span = _agent_span()
    span["attributes"]["agent_detective.contract_params"] = CONTRACT_PARAMS
    assert map_spans([span]).runs[0].contract_params == CONTRACT_PARAMS


def test_contract_params_absent_is_none() -> None:
    assert map_spans([_agent_span()]).runs[0].contract_params is None


def test_contract_params_has_no_resource_fallback() -> None:
    # Per-run data, like artifact_meta: a resource-level value would bind
    # every run under the resource to one node's contract.
    span = _agent_span(
        resource_attributes={"agent_detective.contract_params": CONTRACT_PARAMS}
    )
    assert map_spans([span]).runs[0].contract_params is None


def _tool_span(span_id: str, *, name: str, start: str, attrs: dict, status="ok") -> dict:
    return {
        "trace_id": "t1",
        "span_id": span_id,
        "parent_span_id": "s1",
        "name": name,
        "start_time": start,
        "attributes": {"openinference.span.kind": "TOOL", **attrs},
        "status": status,
    }


def test_tool_calls_digest_two_tools_including_error() -> None:
    import hashlib
    import json

    args = '{"url": "https://example.com/p/1"}'
    ok_tool = _tool_span(
        "s2",
        name="span.fetch",
        start="1752000001000000000",
        attrs={"gen_ai.tool.name": "fetch_page", "input.value": args},
    )
    # No gen_ai.tool.name -> span name; no input.value -> sha over ''.
    err_tool = _tool_span(
        "s3", name="parse_html", start="1752000002000000000", attrs={}, status="error"
    )
    # Input order is error-first: execution order (start time) must win.
    run = map_spans([_agent_span(), err_tool, ok_tool]).runs[0]
    assert run.tool_calls is not None
    assert json.loads(run.tool_calls) == [
        {
            "name": "fetch_page",
            "args_sha": hashlib.sha256(args.encode()).hexdigest()[:12],
            "status": "ok",
        },
        {
            "name": "parse_html",
            "args_sha": hashlib.sha256(b"").hexdigest()[:12],
            "status": "error",
        },
    ]
    # Compact serialization: no whitespace after separators.
    assert ": " not in run.tool_calls and ", " not in run.tool_calls


def test_tool_calls_digest_tiebreaks_on_span_id_at_equal_start() -> None:
    a = _tool_span("s2", name="a", start="1752000001000000000", attrs={})
    b = _tool_span("s3", name="b", start="1752000001000000000", attrs={})
    import json

    run = map_spans([_agent_span(), b, a]).runs[0]
    assert [t["name"] for t in json.loads(run.tool_calls)] == ["a", "b"]


def test_tool_calls_is_none_without_tool_member_spans() -> None:
    llm_child = {
        "trace_id": "t1",
        "span_id": "s2",
        "parent_span_id": "s1",
        "attributes": {"openinference.span.kind": "LLM"},
    }
    run = map_spans([_agent_span(), llm_child]).runs[0]
    assert run.tool_calls is None  # None, never an empty array


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


def test_graph_type_is_resource_service_name() -> None:
    # service.name is the cohort key ingest stores as graph_type.
    span = _agent_span(resource_attributes={"service.name": "generative-simon"})
    result = map_spans([span])
    assert result.graph_types == {"t1": "generative-simon"}


def test_graph_type_is_none_without_service_name() -> None:
    # No resource service.name -> None, never invented; every graph still keyed.
    result = map_spans([_agent_span()])
    assert result.graph_types == {"t1": None}
