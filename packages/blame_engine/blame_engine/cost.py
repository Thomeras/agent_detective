"""Downstream cost: culprits + all descendants in the ORIGINAL graph (spec 3.7).

Union across culprits, deduplicated, including each culprit's own cost.

Cost is optional telemetry: an instrumentation that never sets
``gen_ai.usage.cost`` leaves every node at ``None``. Summing those as zero
reported "this defect cost $0.00" — a measurement-shaped claim about something
nobody measured, and indistinguishable from a genuinely free run. So the sum
runs over the KNOWN costs only, and returns ``None`` when none of the affected
nodes has one. A partial sum (some nodes priced, some not) is still returned:
it is a floor, and reporting the part we do know beats discarding it.
"""

from collections.abc import Iterable

import networkx as nx

from .graph import build_graph
from .types import BlameInput


def downstream_cost(inp: BlameInput, culprit_run_ids: Iterable[str]) -> float | None:
    graph = build_graph(inp)
    affected: set[str] = set()
    for culprit in culprit_run_ids:
        if culprit not in graph:
            continue
        affected.add(culprit)
        affected.update(nx.descendants(graph, culprit))
    # No affected node at all (no culprit, or culprits outside the graph) is a
    # genuine zero: nothing is downstream, so nothing was spent downstream.
    # Unknown only arises when there ARE affected nodes and none carries a cost.
    if not affected:
        return 0.0
    known = [c for n in affected if (c := inp.node_costs.get(n)) is not None]
    if not known:
        return None
    return sum(known)
