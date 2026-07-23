"""Malformed and incomplete input: graceful degradation, never a crash."""

from datetime import datetime, timezone

from otel_mapper import EdgeType, flatten_export_request, map_spans

MAL_TRACE = "eeee0000000000000000000000000005"
ROOT_KEY = f"{MAL_TRACE}:0000000000000a01"
CHILD_KEY = f"{MAL_TRACE}:0000000000000a02"


def test_malformed_payload_does_not_crash(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("malformed.json")))
    # The id-less span and the non-dict entry are skipped; two AGENT spans open runs.
    assert {r.run_key for r in result.runs} == {ROOT_KEY, CHILD_KEY}


def test_malformed_unknown_agent_fields_are_none(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("malformed.json")))
    for run in result.runs:
        assert run.agent_name is None
        assert run.agent_version is None
        assert run.model_name is None
        assert run.prompt_hash is None
        assert run.tokens_in is None
        assert run.tokens_out is None
        assert run.cost_usd is None
        assert run.input is None
        assert run.output is None


def test_malformed_status_and_timestamps(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("malformed.json")))
    by_key = {r.run_key: r for r in result.runs}
    assert by_key[ROOT_KEY].status == "ok"
    assert by_key[CHILD_KEY].status == "failed"  # STATUS_CODE_ERROR on the opener
    # The child's garbage startTimeUnixNano falls back to member-span times.
    assert by_key[CHILD_KEY].start_time == datetime.fromtimestamp(
        1752000001, tz=timezone.utc
    )
    assert by_key[ROOT_KEY].start_time == datetime.fromtimestamp(
        1752000000, tz=timezone.utc
    )


def test_malformed_structural_spawn_edge_kept_with_unknown_names(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("malformed.json")))
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.type is EdgeType.SPAWN
    assert (edge.from_run_key, edge.to_run_key) == (ROOT_KEY, CHILD_KEY)
    assert "agent name unknown" in edge.detection_method


def test_malformed_graph_id_falls_back_to_trace_id(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("malformed.json")))
    assert result.graph_ids == {MAL_TRACE}
    assert all(r.graph_id == MAL_TRACE for r in result.runs)
