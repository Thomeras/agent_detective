"""Propagation path: shortest path in the condensation DAG from the culprit to
the terminal super-node, with super-nodes expanded to members ordered by
end_time (spec 3.7).

Target super-node: the one containing the worst-scored original terminal, or
the flagged terminal when terminal_verdict.bad. Disconnected graph: the path
stays within the culprit's component.
"""

import networkx as nx

from .condense import Condensation, _chron_key
from .graph import terminals
from .types import BlameInput


def _node_score(inp: BlameInput, run_id: str) -> float | None:
    ns = inp.scores.get(run_id)
    return ns.score if ns is not None else None


def _pick_target(
    cond: Condensation, inp: BlameInput, candidate_sids: list[int] | tuple[int, ...]
) -> int:
    """Deterministically pick the target super-node among candidate sinks."""
    cand = set(candidate_sids)
    terms = [t for t in terminals(cond.graph) if cond.node_to_super[t] in cand]
    verdict = inp.terminal_verdict
    if verdict is not None and verdict.bad and terms:
        # Flagged terminal: the last finished original terminal.
        return cond.node_to_super[max(terms, key=lambda t: _chron_key(inp, t))]
    scored_terms = [t for t in terms if _node_score(inp, t) is not None]
    if scored_terms:
        worst = min(scored_terms, key=lambda t: (_node_score(inp, t), _chron_key(inp, t)))
        return cond.node_to_super[worst]
    if terms:
        return cond.node_to_super[max(terms, key=lambda t: _chron_key(inp, t))]
    # No original terminals among candidates (fully cyclic component).
    scored_sinks = [s for s in cand if cond.super_nodes[s].score is not None]
    if scored_sinks:
        return min(
            scored_sinks,
            key=lambda s: (cond.super_nodes[s].score, _chron_key(inp, cond.super_nodes[s].exit_node)),
        )
    return max(cand, key=lambda s: _chron_key(inp, cond.super_nodes[s].exit_node))


_INF = float("inf")


def _step_key(cond: Condensation, inp: BlameInput, sid: int):
    """Ordering of one hop: most-degraded first, unknown last, then chronological.

    Preferring the LOWER score picks the branch that actually carried the damage
    — which is what a propagation path claims to show — and an unknown score is
    not evidence of propagation, so it sorts behind any measured one.
    """
    sn = cond.super_nodes[sid]
    known = 1 if sn.score is None else 0
    return (known, sn.score if sn.score is not None else _INF) + _chron_key(
        inp, sn.exit_node
    )


def _best_path(
    cond: Condensation, inp: BlameInput, start: int, target: int
) -> list[int]:
    """Shortest path start -> target, ties broken DETERMINISTICALLY.

    ``nx.shortest_path`` returns whichever equally-short path its traversal
    reaches first, which on a diamond (one culprit, two branches, one join)
    depends on the order the edges were inserted — i.e. on the order the exporter
    happened to emit spans. The same run then produced two different propagation
    paths, silently, and every golden fixture, judge cassette and cross-run diff
    rests on that not happening.

    Ties are broken by ``_step_key``: among equally short paths, the one through
    the most degraded nodes wins. That is not just a tiebreak — it is the path
    the report means.

    DP over ``cond.topo`` (already a deterministic topological order): the key is
    (hops, per-hop keys) compared lexicographically, and extending two prefixes of
    equal length by the same node preserves their order, so the greedy choice per
    node is optimal.
    """
    # sid -> (hop count, per-hop keys, predecessor on the winning path)
    best: dict[int, tuple[int, tuple, int | None]] = {start: (0, (), None)}
    for sid in cond.topo:
        if sid == start:
            continue
        step = _step_key(cond, inp, sid)
        options = [
            (best[p][0] + 1, best[p][1] + (step,), p)
            # Chronological key, not super-node id: ids are assigned in edge
            # insertion order and are not stable across two encodings of the same
            # run (see _degradation_chains for the same trap).
            for p in sorted(
                cond.dag.predecessors(sid),
                key=lambda s: _chron_key(inp, cond.super_nodes[s].exit_node),
            )
            if p in best
        ]
        if options:
            best[sid] = min(options, key=lambda o: (o[0], o[1]))
    if target not in best:
        return [start]
    path = [target]
    while path[-1] != start:
        path.append(best[path[-1]][2])
    return list(reversed(path))


def propagation_path(inp: BlameInput, cond: Condensation, culprit_run_id: str) -> list[str]:
    start = cond.node_to_super.get(culprit_run_id)
    if start is None:
        return []
    reachable = nx.descendants(cond.dag, start) | {start}
    reachable_sinks = [s for s in cond.sinks if s in reachable]
    if not reachable_sinks:
        sids = [start]
    else:
        target = _pick_target(cond, inp, cond.sinks)
        if target not in reachable:
            # Disconnected graph: stay within the culprit's component.
            target = _pick_target(cond, inp, reachable_sinks)
        sids = _best_path(cond, inp, start, target)
    path: list[str] = []
    for sid in sids:
        path.extend(cond.super_nodes[sid].members)
    return path
