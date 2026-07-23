"""A2A mesh pair: peers exchanging a2a.task_id-correlated spans (build spec 6.1).

A real mesh/P2P pair produces A2A_MESSAGE edges in BOTH directions when
``a2a_detection`` is on. Peer traffic crosses traces (each peer runs in its own
trace), so no SPAWN edge can ever link the peers — the A2A rule is the only
source of structure. With the flag off the same spans document the degraded
mode: the peers still share one execution graph via the correlation header, but
as membership without directed edges.
"""

from otel_mapper import EdgeType, map_spans

MESH_GRAPH_ID = "g-mesh-1"
NORTH_KEY = "aaaa00000000000000000000000000a1:00000000000000a1"
SOUTH_KEY = "bbbb00000000000000000000000000b1:00000000000000b1"


def _agent_span(trace_id: str, span_id: str, name: str) -> dict:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "attributes": {
            "openinference.span.kind": "AGENT",
            "gen_ai.agent.name": name,
            "x-execution-graph-id": MESH_GRAPH_ID,
        },
    }


def _mesh_pair_spans() -> list[dict]:
    """Two peers in separate traces, each sending the other an A2A task.

    Models a market bid exchange: bidder-north asks bidder-south for a quote
    (task-bid-1) while bidder-south asks bidder-north for one (task-bid-2).
    Each request is a CLIENT span carrying ``a2a.task_id`` and
    ``a2a.peer_agent``; membership is shared via ``x-execution-graph-id``.
    """
    return [
        _agent_span("aaaa00000000000000000000000000a1", "00000000000000a1", "bidder-north"),
        {
            "trace_id": "aaaa00000000000000000000000000a1",
            "span_id": "00000000000000a2",
            "parent_span_id": "00000000000000a1",
            "kind": "CLIENT",
            "attributes": {"a2a.task_id": "task-bid-1", "a2a.peer_agent": "bidder-south"},
        },
        _agent_span("bbbb00000000000000000000000000b1", "00000000000000b1", "bidder-south"),
        {
            "trace_id": "bbbb00000000000000000000000000b1",
            "span_id": "00000000000000b2",
            "parent_span_id": "00000000000000b1",
            "kind": "CLIENT",
            "attributes": {"a2a.task_id": "task-bid-2", "a2a.peer_agent": "bidder-north"},
        },
    ]


def test_mesh_pair_yields_a2a_edges_both_directions() -> None:
    result = map_spans(_mesh_pair_spans(), a2a_detection=True)

    assert {r.run_key for r in result.runs} == {NORTH_KEY, SOUTH_KEY}
    assert len(result.edges) == 2
    assert all(e.type is EdgeType.A2A_MESSAGE for e in result.edges)
    # A real mesh pair: influence flows both ways. Each CLIENT span produces
    # peer -> caller (the peer's response flows back into the caller).
    assert {(e.from_run_key, e.to_run_key) for e in result.edges} == {
        (SOUTH_KEY, NORTH_KEY),  # north's request span: south's answer -> north
        (NORTH_KEY, SOUTH_KEY),  # south's request span: north's answer -> south
    }
    # Every edge records which rule fired.
    for edge in result.edges:
        assert "rule=a2a_message" in edge.detection_method
        assert "a2a.task_id" in edge.detection_method


def test_mesh_pair_flag_off_degrades_to_membership_without_edges() -> None:
    # Degraded mode (A2A_DETECTION off, the pre-mesh default): the very same
    # spans still land in one execution graph via the correlation header, but
    # no directed edges exist — blame cannot be localised between the peers.
    result = map_spans(_mesh_pair_spans())

    assert {r.run_key for r in result.runs} == {NORTH_KEY, SOUTH_KEY}
    assert result.graph_ids == {MESH_GRAPH_ID}
    assert {r.graph_id for r in result.runs} == {MESH_GRAPH_ID}
    assert result.edges == []
