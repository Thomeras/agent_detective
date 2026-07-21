"""Algorithm 3: cut-point candidates, shadowing, selection (spec 3.5).

A super-node is a candidate when it is below threshold OR dropped
significantly from the best known predecessor base, AND the drop is not
merely inherited (drop >= min_drop, unless the base itself is unknown).
Shadowing drops any candidate that has another candidate among its ancestors
in the condensation DAG, leaving independent origins only.
"""

from dataclasses import dataclass

import networkx as nx

from .condense import Condensation, condense
from .types import BlameInput


@dataclass(frozen=True)
class Candidate:
    super_id: int
    run_id: str                 # exit node of the super-node
    score: float
    base: float | None          # best known predecessor score; 1.0 for sources
    drop: float | None          # max(0, base - score); None when base unknown
    unknown_upstream: bool      # unknown super-node anywhere in the upstream cone
    is_source: bool             # source of the condensation DAG
    iterations: int             # SCC size
    end_time: float | None      # exit-node end time (ordering tie-break)


@dataclass(frozen=True)
class CutAnalysis:
    candidates: tuple[Candidate, ...]     # unshadowed, deterministic topo order
    unscored_super_ids: tuple[int, ...]
    had_significant_drop: bool            # any scored node with drop >= gap_threshold


def _analyze(cond: Condensation, inp: BlameInput) -> CutAnalysis:
    cfg = inp.config
    dag = cond.dag
    candidates: list[Candidate] = []
    unscored: list[int] = []
    had_significant_drop = False

    for sid in cond.topo:
        sn = cond.super_nodes[sid]
        s = sn.score
        if s is None:
            unscored.append(sid)          # unknown is never a culprit
            continue
        known_pred_scores = [
            cond.super_nodes[p].score
            for p in dag.predecessors(sid)
            if cond.super_nodes[p].score is not None
        ]
        if known_pred_scores:
            base = max(known_pred_scores)
        elif dag.in_degree(sid) == 0:
            base = 1.0                    # source baseline
        else:
            base = None                   # all predecessors unknown
        drop = max(0.0, base - s) if base is not None else None

        is_below = s < cfg.threshold                      # criterion (a)
        is_drop = drop is not None and drop >= cfg.gap_threshold  # criterion (b)
        if is_drop:
            had_significant_drop = True
        if (is_below or is_drop) and (drop is None or drop >= cfg.min_drop):
            # Below threshold with drop < min_drop is inherited degradation:
            # the node is on the path, not the origin.
            candidates.append(
                Candidate(
                    super_id=sid,
                    run_id=sn.exit_node,
                    score=s,
                    base=base,
                    drop=drop,
                    unknown_upstream=any(
                        cond.super_nodes[a].score is None for a in nx.ancestors(dag, sid)
                    ),
                    is_source=dag.in_degree(sid) == 0,
                    iterations=sn.iterations,
                    end_time=inp.node_end_times.get(sn.exit_node),
                )
            )

    # Shadowing: drop candidates with another candidate among their ancestors.
    candidate_ids = {c.super_id for c in candidates}
    final = [
        c
        for c in candidates
        if not any(a in candidate_ids for a in nx.ancestors(dag, c.super_id))
    ]
    return CutAnalysis(tuple(final), tuple(unscored), had_significant_drop)


def select_candidates(inp: BlameInput) -> list[Candidate]:
    """Unshadowed cut-point candidates in deterministic topological order."""
    return list(_analyze(condense(inp), inp).candidates)
