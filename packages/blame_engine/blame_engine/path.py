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
        sids = nx.shortest_path(cond.dag, start, target)
    path: list[str] = []
    for sid in sids:
        path.extend(cond.super_nodes[sid].members)
    return path
