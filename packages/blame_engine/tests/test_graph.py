"""Graph helper behavior: sources/terminals classification (spec 3.1)."""

from blame_engine.graph import build_graph, sources, terminals


def test_sources_and_terminals(mk) -> None:
    inp = mk(
        nodes=["a", "b", "c", "d"],
        edges=[("a", "c"), ("b", "c"), ("c", "d")],
    )
    graph = build_graph(inp)
    assert sources(graph) == ["a", "b"]
    assert terminals(graph) == ["d"]


def test_sources_and_terminals_with_cycle(mk) -> None:
    inp = mk(nodes=["s", "a", "b"], edges=[("s", "a"), ("a", "b"), ("b", "a")])
    graph = build_graph(inp)
    # Nodes inside the cycle have both in- and out-edges.
    assert sources(graph) == ["s"]
    assert terminals(graph) == []
