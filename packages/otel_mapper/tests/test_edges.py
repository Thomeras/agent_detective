"""Edge detection rules (build spec 6.1): SPAWN, TOOL_DELEGATION, A2A, header."""

from datetime import datetime, timezone

import pytest

from otel_mapper import EdgeType, UnresolvedDelegation, flatten_export_request, map_spans

SPAWN_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
ORCH_KEY = f"{SPAWN_TRACE}:00000000000000a1"
SCRAPER_KEY = f"{SPAWN_TRACE}:00000000000000b1"
SCRAPER_RETRY_KEY = f"{SPAWN_TRACE}:00000000000000b3"

TOOL_TRACE = "1a2b3c4d5e6f708192a3b4c5d6e7f801"
TOOL_ORCH_KEY = f"{TOOL_TRACE}:0000000000000c01"
TOOL_SCRAPER_KEY = f"{TOOL_TRACE}:0000000000000c02"
TOOL_COMPLIANCE_KEY = f"{TOOL_TRACE}:0000000000000c03"

A2A_FRONTEND_KEY = "aaaa0000000000000000000000000001:00000000000000f1"
A2A_ANALYST_KEY = "bbbb0000000000000000000000000002:00000000000000d1"


def test_spawn_pipeline_runs(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("spawn_pipeline.json")))
    by_key = {r.run_key: r for r in result.runs}
    assert set(by_key) == {ORCH_KEY, SCRAPER_KEY, SCRAPER_RETRY_KEY}

    orch = by_key[ORCH_KEY]
    assert orch.agent_name == "orchestrator"  # from resource attributes
    assert orch.agent_version == "1.4.0"
    assert orch.model_name == "gpt-4o-mini"  # member LLM span fallback (semconv)
    assert orch.prompt_hash == "ab12cd34ef56"  # span attribute
    assert orch.artifact_meta == (
        '[{"path":"out/products.json","size":2048,"sha256":"deadbeefcafe",'
        '"declared_ext":"json","detected_kind":"json","parse_ok":true,"nonempty":true}]'
    )  # raw opener-span attribute, passed through verbatim
    assert (orch.tokens_in, orch.tokens_out) == (1200, 300)  # AGENT span wins
    assert orch.cost_usd == pytest.approx(0.012)
    assert orch.input == "Find three products and translate them."
    assert orch.output == "Published 3 localized products."
    assert orch.status == "ok"
    assert orch.graph_id == "g-spawn-1"
    assert orch.start_time == datetime.fromtimestamp(1752000000, tz=timezone.utc)
    assert orch.end_time == datetime.fromtimestamp(1752000009, tz=timezone.utc)

    scraper = by_key[SCRAPER_KEY]
    assert scraper.agent_name == "scraper-agent"  # span attribute
    assert scraper.agent_version == "0.9.2"  # resource attribute fallback
    assert scraper.model_name == "claude-haiku-4-5"  # resource attribute fallback
    assert scraper.prompt_hash is None  # never invented when absent
    assert scraper.artifact_meta is None  # never invented when absent
    assert (scraper.tokens_in, scraper.tokens_out) == (800, 200)  # children sum
    assert scraper.cost_usd == pytest.approx(0.004)

    assert scraper.tool_calls is None  # no TOOL member spans -> None

    retry = by_key[SCRAPER_RETRY_KEY]
    assert retry.agent_name == "scraper-agent"
    assert retry.tokens_in is None and retry.cost_usd is None
    # Two TOOL member spans in execution order: named tool with hashed args,
    # then an errored tool falling back to the span name with sha256('').
    assert retry.tool_calls == (
        '[{"name":"fetch_page","args_sha":"5bba4ef7e89c","status":"ok"},'
        '{"name":"scraper.parse_html","args_sha":"e3b0c44298fc","status":"error"}]'
    )
    assert retry.status == "failed"  # the errored TOOL span fails the run


def test_spawn_pipeline_edges(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("spawn_pipeline.json")))
    assert result.graph_ids == {"g-spawn-1"}
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.type is EdgeType.SPAWN
    assert edge.from_run_key == ORCH_KEY
    assert edge.to_run_key == SCRAPER_KEY
    assert "openinference.span.kind=AGENT" in edge.detection_method
    # The same-agent nested AGENT span (scraper retry) must not produce an edge.
    assert all(e.to_run_key != SCRAPER_RETRY_KEY for e in result.edges)


def test_runs_sorted_by_start_time(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("spawn_pipeline.json")))
    assert [r.run_key for r in result.runs] == [ORCH_KEY, SCRAPER_KEY, SCRAPER_RETRY_KEY]


def test_tool_delegation_edges(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("tool_delegation.json")))
    by_type: dict[EdgeType, list] = {}
    for e in result.edges:
        by_type.setdefault(e.type, []).append(e)

    spawn_pairs = {(e.from_run_key, e.to_run_key) for e in by_type[EdgeType.SPAWN]}
    assert spawn_pairs == {
        (TOOL_ORCH_KEY, TOOL_SCRAPER_KEY),
        (TOOL_ORCH_KEY, TOOL_COMPLIANCE_KEY),
    }

    # Two tool spans target scraper-agent; the edge is deduplicated to one.
    tool_edges = by_type[EdgeType.TOOL_DELEGATION]
    assert len(tool_edges) == 1
    tool_edge = tool_edges[0]
    # Direction: target -> caller, because the target's output flows back.
    assert tool_edge.from_run_key == TOOL_SCRAPER_KEY
    assert tool_edge.to_run_key == TOOL_COMPLIANCE_KEY
    assert "gen_ai.tool.target_agent='scraper-agent'" in tool_edge.detection_method

    # The tool span targeting the nonexistent ghost-agent yields no edge.
    assert all("ghost-agent" not in e.detection_method for e in result.edges)


def test_unresolved_delegation_recorded(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("tool_delegation.json")))
    # The resolvable target (scraper-agent) became an edge above; only the
    # unresolvable one is recorded, with the identity needed to retry later.
    assert result.unresolved_delegations == [
        UnresolvedDelegation(
            owner_run_key=TOOL_COMPLIANCE_KEY,
            target_name="ghost-agent",
            trace_id=TOOL_TRACE,
            span_id="0000000000000c05",
        )
    ]


def test_tool_delegation_error_propagates_to_run_status(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("tool_delegation.json")))
    by_key = {r.run_key: r for r in result.runs}
    assert by_key[TOOL_COMPLIANCE_KEY].status == "failed"  # errored member span
    assert by_key[TOOL_SCRAPER_KEY].status == "ok"


def test_a2a_edges_require_feature_flag(fixture_json) -> None:
    spans = flatten_export_request(fixture_json("a2a_message.json"))

    off = map_spans(spans)
    assert off.edges == []

    on = map_spans(spans, a2a_detection=True)
    assert len(on.edges) == 1
    edge = on.edges[0]
    assert edge.type is EdgeType.A2A_MESSAGE
    # CLIENT-side span: peer -> caller, the peer's response flows back.
    assert edge.from_run_key == A2A_ANALYST_KEY
    assert edge.to_run_key == A2A_FRONTEND_KEY
    assert "a2a.task_id" in edge.detection_method


def test_a2a_runs_share_graph_via_header(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("a2a_message.json")))
    assert result.graph_ids == {"g-a2a-1"}
    assert {r.graph_id for r in result.runs} == {"g-a2a-1"}


def _flat_agent(trace_id: str, span_id: str, name: str, **extra):
    span = {
        "trace_id": trace_id,
        "span_id": span_id,
        "attributes": {"openinference.span.kind": "AGENT", "gen_ai.agent.name": name},
    }
    span.update(extra)
    return span


def test_a2a_agent_card_fetch_rule_fires_without_task_id() -> None:
    spans = [
        _flat_agent("t1", "f1", "frontend-agent"),
        {
            "trace_id": "t1",
            "span_id": "c1",
            "parent_span_id": "f1",
            "kind": "CLIENT",
            "attributes": {
                "http.request.method": "GET",
                "url.full": "https://agents.internal/analyst/.well-known/agent.json",
                "a2a.peer_agent": "analyst-agent",
            },
        },
        _flat_agent("t2", "a1", "analyst-agent"),
    ]
    result = map_spans(spans, a2a_detection=True)
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.type is EdgeType.A2A_MESSAGE
    assert (edge.from_run_key, edge.to_run_key) == ("t2:a1", "t1:f1")
    assert ".well-known/agent.json" in edge.detection_method


def test_a2a_server_span_flips_direction() -> None:
    spans = [
        _flat_agent("t1", "f1", "frontend-agent"),
        _flat_agent("t2", "a1", "analyst-agent"),
        {
            "trace_id": "t2",
            "span_id": "s1",
            "parent_span_id": "a1",
            "kind": "SERVER",
            "attributes": {"a2a.task_id": "task-1", "a2a.peer_agent": "frontend-agent"},
        },
    ]
    result = map_spans(spans, a2a_detection=True)
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert (edge.from_run_key, edge.to_run_key) == ("t2:a1", "t1:f1")


def test_a2a_unresolvable_peer_yields_no_edge() -> None:
    spans = [
        _flat_agent("t1", "f1", "frontend-agent"),
        {
            "trace_id": "t1",
            "span_id": "c1",
            "parent_span_id": "f1",
            "kind": "CLIENT",
            "attributes": {"a2a.task_id": "task-1"},  # no a2a.peer_agent
        },
    ]
    assert map_spans(spans, a2a_detection=True).edges == []


def test_correlation_header_groups_membership_without_direction(fixture_json) -> None:
    result = map_spans(flatten_export_request(fixture_json("correlation_header.json")))
    # Membership: two traces, one execution graph.
    assert result.graph_ids == {"g-corr-1"}
    assert {r.graph_id for r in result.runs} == {"g-corr-1"}
    assert {r.agent_name for r in result.runs} == {"researcher", "writer"}
    # The header says nothing about who called whom: no edges are invented.
    assert result.edges == []
