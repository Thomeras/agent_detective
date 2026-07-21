"""Original DiGraph construction and basic node classifications."""

import networkx as nx

from .types import BlameInput


def build_graph(inp: BlameInput) -> nx.DiGraph:
    """Build the original directed graph. Cycles are allowed and never raise."""
    graph = nx.DiGraph()
    graph.add_nodes_from(inp.nodes)
    graph.add_edges_from(inp.edges)
    return graph


def sources(graph: nx.DiGraph) -> list[str]:
    """Nodes with no incoming edges, sorted by run_id for determinism."""
    return sorted((n for n in graph.nodes if graph.in_degree(n) == 0), key=str)


def terminals(graph: nx.DiGraph) -> list[str]:
    """Nodes with no outgoing edges, sorted by run_id for determinism."""
    return sorted((n for n in graph.nodes if graph.out_degree(n) == 0), key=str)
