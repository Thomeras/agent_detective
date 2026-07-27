"""Original DiGraph construction and basic node classifications."""

import networkx as nx

from .types import BlameInput


def build_graph(inp: BlameInput) -> nx.DiGraph:
    """Build the original directed graph. Cycles are allowed and never raise.

    Edges naming a run that is not in ``nodes`` are ignored — the same tolerance
    ``topology.classify_topology`` already applies. ``add_edges_from`` would
    otherwise CREATE the missing endpoint, and a phantom node with no score reads
    downstream as a genuinely unknown one: it lands in ``unscored_run_ids``, in
    the score map as ``null``, and blocks ``composition_failure`` as a node that
    "could hide the culprit". A dangling edge is an instrumentation artifact, not
    an unobserved agent.
    """
    graph = nx.DiGraph()
    graph.add_nodes_from(inp.nodes)
    known = set(inp.nodes)
    graph.add_edges_from((u, v) for u, v in inp.edges if u in known and v in known)
    return graph


def sources(graph: nx.DiGraph) -> list[str]:
    """Nodes with no incoming edges, sorted by run_id for determinism."""
    return sorted((n for n in graph.nodes if graph.in_degree(n) == 0), key=str)


def terminals(graph: nx.DiGraph) -> list[str]:
    """Nodes with no outgoing edges, sorted by run_id for determinism."""
    return sorted((n for n in graph.nodes if graph.out_degree(n) == 0), key=str)
