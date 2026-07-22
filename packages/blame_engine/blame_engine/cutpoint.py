"""Algorithm 3: edge-drop origins, shadowing, selection (spec 3.5, revised).

The cut point is where quality *broke*, not merely where it is low. An **origin**
is a node whose score dropped past ``gap_threshold`` from a **healthy**
predecessor (``base >= threshold``) — quality was fine going in and broke here —
or a degraded **source** whose degradation is not immediately cured by a healthy
successor (bad from the observable start). A node that merely inherited low
quality is on the path, not the cause; a node that faithfully processed already
-flawed input (``input_flawed``) is a propagation point, never an origin.

Shadowing keeps only the earliest origin on each branch (an origin with an
ancestor origin is downstream degradation, not an independent cause).
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
    base: float | None          # best known predecessor score; None for sources
    drop: float | None          # max(0, base - score); None when base unknown
    unknown_upstream: bool      # unknown super-node anywhere in the upstream cone
    is_source: bool             # source of the condensation DAG
    iterations: int             # SCC size
    end_time: float | None      # exit-node end time (ordering tie-break)
    observed_drop: bool = False  # dropped from a healthy, observed predecessor


@dataclass(frozen=True)
class CutAnalysis:
    candidates: tuple[Candidate, ...]     # unshadowed origins, deterministic topo order
    unscored_super_ids: tuple[int, ...]
    had_significant_drop: bool            # any origin with an observed healthy-drop


def _input_flawed(inp: BlameInput, run_id: str) -> bool:
    ns = inp.scores.get(run_id)
    return ns is not None and ns.input_flawed is True


def _analyze(cond: Condensation, inp: BlameInput) -> CutAnalysis:
    cfg = inp.config
    dag = cond.dag
    drop_origins: list[Candidate] = []       # dropped from a healthy predecessor
    propagating: list[Candidate] = []        # degraded boundary that spreads
    recovered_boundaries: list[Candidate] = []  # degraded boundary cured downstream
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
        observed = bool(known_pred_scores)  # base from a real predecessor
        is_source = dag.in_degree(sid) == 0
        # base for display/evidence: real predecessor, else the 1.0 source
        # baseline (assumed). The observed_drop check below never uses the
        # assumed baseline — only a real predecessor proves quality "broke here".
        if observed:
            base = max(known_pred_scores)
        elif is_source:
            base = 1.0
        else:
            base = None
        drop = max(0.0, base - s) if base is not None else None
        degraded = s < cfg.threshold

        # Origin criterion (a): dropped past the gap from a HEALTHY, *observed*
        # predecessor — quality was fine going in and broke here.
        observed_drop = (
            observed
            and base >= cfg.threshold
            and drop is not None
            and drop >= cfg.gap_threshold
            and drop >= cfg.min_drop
        )
        if observed_drop:
            had_significant_drop = True

        # A "boundary" has no observed (scored) predecessor: a source, or a node
        # whose predecessors are all unknown. A degraded boundary is where the
        # observable degradation begins. It "recovered" if every successor is
        # healthy — then its low quality was transient (a spurious low), not a
        # spreading cause; no successors means it is itself the failure point.
        is_boundary = not observed
        succ_scores = [cond.super_nodes[x].score for x in dag.successors(sid)]
        recovered = bool(succ_scores) and all(
            x is not None and x >= cfg.threshold for x in succ_scores
        )

        # A node that faithfully processed already-flawed input is a propagation
        # point, never an origin (handled as root_cause_external in blame.py).
        if _input_flawed(inp, sn.exit_node):
            continue
        cand = Candidate(
            super_id=sid,
            run_id=sn.exit_node,
            score=s,
            base=base,
            drop=drop,
            unknown_upstream=any(
                cond.super_nodes[a].score is None for a in nx.ancestors(dag, sid)
            ),
            is_source=is_source,
            iterations=sn.iterations,
            end_time=inp.node_end_times.get(sn.exit_node),
            observed_drop=observed_drop,
        )
        if observed_drop:
            drop_origins.append(cand)
        elif is_boundary and degraded and not recovered:
            propagating.append(cand)
        elif is_boundary and degraded:
            recovered_boundaries.append(cand)

    # A recovered (transient-low) boundary is the culprit only when nothing else
    # explains the failure. A real downstream drop-origin always wins over it —
    # that is the "blame the node that broke, not the orchestrator" fix.
    origins = drop_origins + propagating
    if not origins:
        origins = recovered_boundaries

    # Shadowing: keep only the earliest origin on each branch — a downstream
    # origin with an ancestor origin is inherited degradation, not a new cause.
    origin_ids = {c.super_id for c in origins}
    final = [
        c
        for c in origins
        if not any(a in origin_ids for a in nx.ancestors(dag, c.super_id))
    ]
    return CutAnalysis(tuple(final), tuple(unscored), had_significant_drop)


def select_candidates(inp: BlameInput) -> list[Candidate]:
    """Unshadowed cut-point origins in deterministic topological order."""
    return list(_analyze(condense(inp), inp).candidates)
