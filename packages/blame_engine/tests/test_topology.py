"""Per-archetype regression locks for the frozen topology contract.

One test per primary class, plus the tie-break order, attributes,
articulation points on a bridge graph, tolerance to unknown nodes, and the
disconnected instrumentation note surfacing in find_blame WITHOUT changing
the verdict.
"""

from blame_engine import find_blame
from conftest import note_of
from blame_engine.topology import classify_topology


def test_single_node() -> None:
    t = classify_topology(["a"], [])
    assert t["primary"] == "single_node"
    assert t["node_count"] == 1
    assert t["edge_count"] == 0
    assert t["components"] == 1
    assert t["depth"] == 1
    assert t["articulation_points"] == []  # n < 3


def test_pipeline_simple_path() -> None:
    t = classify_topology(["a", "b", "c"], [("a", "b"), ("b", "c")])
    assert t["primary"] == "pipeline"
    assert t["depth"] == 3
    assert t["max_fan_out"] == 1
    assert t["scc_count"] == 0
    assert t["bidirectional_pairs"] == 0
    # b is the bridge on the undirected view.
    assert t["articulation_points"] == ["b"]


def test_star_pure() -> None:
    t = classify_topology(
        ["root", "s1", "s2", "s3"],
        [("root", "s1"), ("root", "s2"), ("root", "s3")],
    )
    assert t["primary"] == "star"
    assert t["max_fan_out"] == 3
    assert t["depth"] == 2


def test_star_with_aggregator_sink() -> None:
    # Root fans out to spokes; ONE extra aggregator sink all spokes feed.
    t = classify_topology(
        ["root", "s1", "s2", "agg"],
        [("root", "s1"), ("root", "s2"), ("s1", "agg"), ("s2", "agg")],
    )
    assert t["primary"] == "star"
    assert t["depth"] == 3


def test_hierarchy_tree_depth_3() -> None:
    # Tree from a single root, depth 3 (root -> child -> grandchild).
    t = classify_topology(
        ["root", "c1", "c2", "g1", "g2"],
        [("root", "c1"), ("root", "c2"), ("c1", "g1"), ("c1", "g2")],
    )
    assert t["primary"] == "hierarchy"
    assert t["depth"] == 3


def test_depth_2_tree_is_star_not_hierarchy() -> None:
    # Tie-break: a rooted tree of depth 2 matches the star rule first, and
    # would fail hierarchy's depth >= 3 gate anyway.
    t = classify_topology(["r", "a", "b"], [("r", "a"), ("r", "b")])
    assert t["primary"] == "star"
    assert t["depth"] == 2


def test_dag_fallback() -> None:
    # Acyclic, connected, but neither path nor star nor rooted tree
    # (c has in-degree 2 and the extra b->c edge breaks the star shape).
    t = classify_topology(
        ["a", "b", "c", "d"],
        [("a", "b"), ("a", "c"), ("b", "c"), ("c", "d")],
    )
    assert t["primary"] == "dag"
    assert t["scc_count"] == 0
    assert t["depth"] == 4  # a -> b -> c -> d


def test_pipeline_with_feedback() -> None:
    # One retry loop inside an otherwise linear chain: condensation is a
    # simple path a -> {b,c} -> d.
    t = classify_topology(
        ["a", "b", "c", "d"],
        [("a", "b"), ("b", "c"), ("c", "b"), ("c", "d")],
    )
    assert t["primary"] == "pipeline_with_feedback"
    assert t["scc_count"] == 1
    assert t["bidirectional_pairs"] == 1
    assert t["depth"] == 3  # condensation: a -> [bc] -> d


def test_cyclic_graph_when_condensation_not_a_path() -> None:
    # One SCC whose condensation fans out to two sinks: cyclic_graph.
    t = classify_topology(
        ["a", "b", "c", "d"],
        [("a", "b"), ("b", "a"), ("a", "c"), ("b", "d")],
    )
    assert t["primary"] == "cyclic_graph"
    assert t["scc_count"] == 1
    assert t["bidirectional_pairs"] == 1


def test_mesh_by_bidirectional_pairs_beats_pipeline_with_feedback() -> None:
    # Whole graph is one SCC (condensation = trivial simple path), but TWO
    # bidirectional pairs make it mesh — mesh wins the tie-break.
    t = classify_topology(
        ["a", "b", "c"],
        [("a", "b"), ("b", "a"), ("b", "c"), ("c", "b")],
    )
    assert t["primary"] == "mesh"
    assert t["bidirectional_pairs"] == 2
    assert t["scc_count"] == 1


def test_mesh_by_dense_scc_of_4() -> None:
    # Only one bidirectional pair, but an SCC of size 4 with internal edge
    # density 6/12 = 0.5 — the density route to mesh.
    t = classify_topology(
        ["a", "b", "c", "d"],
        [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a"), ("a", "c"), ("c", "a")],
    )
    assert t["primary"] == "mesh"
    assert t["bidirectional_pairs"] == 1


def test_plain_4_cycle_is_not_mesh() -> None:
    # Density 4/12 < 0.5 and no bidirectional pairs: the ring condenses to a
    # single trivial-path node -> pipeline_with_feedback.
    t = classify_topology(
        ["a", "b", "c", "d"],
        [("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")],
    )
    assert t["primary"] == "pipeline_with_feedback"
    assert t["bidirectional_pairs"] == 0


def test_disconnected_primary_and_attributes() -> None:
    t = classify_topology(["a", "b", "c", "d"], [("a", "b"), ("c", "d")])
    assert t["primary"] == "disconnected"
    assert t["components"] == 2


def test_articulation_points_on_bridge_graph() -> None:
    # Diamond a->{b,c}->d plus a tail d->e: d is the bridge to e on the
    # undirected view; removing any other node keeps the graph connected.
    t = classify_topology(
        ["a", "b", "c", "d", "e"],
        [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "e")],
    )
    assert t["articulation_points"] == ["d"]


def test_unknown_nodes_in_edges_are_ignored() -> None:
    t = classify_topology(
        ["a", "b"], [("a", "b"), ("a", "ghost"), ("ghost", "b")]
    )
    assert t["primary"] == "pipeline"
    assert t["node_count"] == 2
    assert t["edge_count"] == 1


def test_find_blame_disconnected_note_without_verdict_change(mk) -> None:
    # Two components; the break lives entirely in one of them. Classification
    # must stay EXACTLY as before (cut_point at 'b') — the topology layer only
    # adds the instrumentation-quality note and the advisory evidence dict.
    inp = mk(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "b"), ("c", "d")],
        scores={"a": 1.0, "b": 0.2, "c": 1.0, "d": 0.9},
    )
    report = find_blame(inp)

    assert report.report_type == "cut_point"
    assert report.culprit_run_ids == ["b"]
    assert report.evidence.topology["primary"] == "disconnected"
    assert note_of(report, "topology")["components"] == 2


def test_find_blame_connected_graph_has_no_topology_note(mk) -> None:
    inp = mk(
        nodes=["a", "b", "c"],
        edges=[("a", "b"), ("b", "c")],
        scores={"a": 1.0, "b": 0.9, "c": 0.2},
    )
    report = find_blame(inp)

    assert report.evidence.topology["primary"] == "pipeline"
    assert note_of(report, "topology") is None
