"""Downstream cost: culprits + all descendants in the ORIGINAL graph (spec 3.7).

Union across culprits, deduplicated, including each culprit's own cost.
"""

from collections.abc import Iterable

import networkx as nx

from .graph import build_graph
from .types import BlameInput


def downstream_cost(inp: BlameInput, culprit_run_ids: Iterable[str]) -> float:
    graph = build_graph(inp)
    affected: set[str] = set()
    for culprit in culprit_run_ids:
        if culprit not in graph:
            continue
        affected.add(culprit)
        affected.update(nx.descendants(graph, culprit))
    return sum(inp.node_costs.get(n, 0.0) for n in affected)
