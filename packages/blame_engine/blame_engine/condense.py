"""Algorithm 1: SCC condensation with super-node scoring (spec 3.3).

Cycles never raise and never auto-classify loop_detected; cyclic parts are
condensed into super-nodes. A super-node's score is the score of its exit node
(the last finished member), because that is what flows downstream.
"""

import heapq
from dataclasses import dataclass

import networkx as nx

from .graph import build_graph
from .types import BlameInput

_NEG_INF = float("-inf")


@dataclass(frozen=True)
class SuperNode:
    id: int
    members: tuple[str, ...]        # chronological: (end_time, run_id) ascending
    exit_node: str                  # last finished member (max end_time, then run_id)
    score: float | None             # exit-node score; None = UNKNOWN
    min_member_score: float | None  # min of known member scores; evidence only
    iterations: int                 # number of members
    has_self_loop: bool             # single member with an edge to itself


@dataclass(frozen=True)
class Condensation:
    graph: nx.DiGraph               # original graph
    dag: nx.DiGraph                 # condensation DAG of super-nodes
    super_nodes: dict[int, SuperNode]
    node_to_super: dict[str, int]
    sources: tuple[int, ...]        # in deterministic topo order
    sinks: tuple[int, ...]          # in deterministic topo order
    topo: tuple[int, ...]           # deterministic topological order


def _chron_key(inp: BlameInput, run_id: str) -> tuple[float, str]:
    """Chronological sort key: missing end_time sorts first, ties by run_id."""
    return (inp.node_end_times.get(run_id, _NEG_INF), run_id)


def condense(inp: BlameInput) -> Condensation:
    graph = build_graph(inp)
    dag = nx.condensation(graph)

    super_nodes: dict[int, SuperNode] = {}
    node_to_super: dict[str, int] = {}
    for sid, data in dag.nodes(data=True):
        members = tuple(sorted(data["members"], key=lambda m: _chron_key(inp, m)))
        exit_node = members[-1]
        ns = inp.scores.get(exit_node)
        score = ns.score if ns is not None else None
        known = [
            inp.scores[m].score
            for m in members
            if inp.scores.get(m) is not None and inp.scores[m].score is not None
        ]
        super_nodes[sid] = SuperNode(
            id=sid,
            members=members,
            exit_node=exit_node,
            score=score,
            min_member_score=min(known) if known else None,
            iterations=len(members),
            has_self_loop=len(members) == 1 and graph.has_edge(members[0], members[0]),
        )
        for m in members:
            node_to_super[m] = sid

    # Deterministic topological order (Kahn) with tie-break: earlier exit-node
    # end_time first, then run_id (spec 3.5).
    indeg = {n: dag.in_degree(n) for n in dag.nodes}
    heap: list[tuple[float, str, int]] = [
        (*_chron_key(inp, super_nodes[n].exit_node), n) for n, d in indeg.items() if d == 0
    ]
    heapq.heapify(heap)
    topo: list[int] = []
    while heap:
        _, _, n = heapq.heappop(heap)
        topo.append(n)
        for succ in sorted(dag.successors(n)):
            indeg[succ] -= 1
            if indeg[succ] == 0:
                heapq.heappush(heap, (*_chron_key(inp, super_nodes[succ].exit_node), succ))

    topo_t = tuple(topo)
    return Condensation(
        graph=graph,
        dag=dag,
        super_nodes=super_nodes,
        node_to_super=node_to_super,
        sources=tuple(n for n in topo_t if dag.in_degree(n) == 0),
        sinks=tuple(n for n in topo_t if dag.out_degree(n) == 0),
        topo=topo_t,
    )
