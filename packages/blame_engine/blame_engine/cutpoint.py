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
    # Every scored successor is healthy: the degradation here was transient /
    # compensated downstream (a recovered boundary). With a healthy terminal this
    # is a near-miss, not a live quality break.
    recovered: bool = False
    # True when `base` is the ASSUMED 1.0 source-like baseline, not a real scored
    # predecessor. An assumed baseline may inform confidence (a clean handoff is a
    # justified assumption, stated in candidacy) but must NEVER be presented as an
    # observed drop in the evidence — "-0.85 from best-scored predecessor" against
    # a fictional 1.00 is a fabricated number.
    base_assumed: bool = False
    # Non-empty when this candidate was derived from a cumulative degradation
    # chain (slow erosion) rather than one sharp edge drop. Holds the full chain
    # of run_ids, healthy head first; `drop` then carries the CUMULATIVE decline.
    cumulative_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class DegradationChain:
    """A monotone decline over >= cum_min_edges consecutive edges starting from a
    healthy node, whose total drop reaches cum_drop_threshold. No single edge
    crossed gap_threshold — the quality eroded rather than broke — which is a
    distinct, reportable origin signal ("no significant drops" would be a lie)."""

    super_ids: tuple[int, ...]
    run_ids: tuple[str, ...]        # exit nodes, healthy head first
    scores: tuple[float, ...]
    cumulative_drop: float


@dataclass(frozen=True)
class CutAnalysis:
    candidates: tuple[Candidate, ...]     # unshadowed origins, deterministic topo order
    unscored_super_ids: tuple[int, ...]
    had_significant_drop: bool            # any origin with an observed healthy-drop
    degradation_chains: tuple[DegradationChain, ...] = ()


def _input_flawed(inp: BlameInput, run_id: str) -> bool:
    ns = inp.scores.get(run_id)
    return ns is not None and ns.input_flawed is True


def _degradation_chains(cond: Condensation, inp: BlameInput) -> list[DegradationChain]:
    """Greedy maximal decline chains over the condensation DAG.

    From each healthy scored node, repeatedly follow the successor with the
    largest per-edge decline (>= cum_step_min), and record the chain when it
    spans >= cum_min_edges edges and its cumulative drop reaches
    cum_drop_threshold. Nodes already inside a recorded chain do not start a new
    (sub-)chain, so overlapping suffixes are not double-reported.
    """
    cfg = inp.config
    chains: list[DegradationChain] = []
    consumed: set[int] = set()
    for sid in cond.topo:
        if sid in consumed:
            continue
        head_score = cond.super_nodes[sid].score
        if head_score is None or head_score < cfg.threshold:
            continue
        path = [sid]
        cur, cur_score = sid, head_score
        while True:
            best, best_score = None, None
            for succ in sorted(cond.dag.successors(cur)):
                sc = cond.super_nodes[succ].score
                if sc is not None and cur_score - sc >= cfg.cum_step_min:
                    if best is None or sc < best_score:
                        best, best_score = succ, sc
            if best is None:
                break
            path.append(best)
            cur, cur_score = best, best_score
        cumulative = head_score - cur_score
        if len(path) - 1 >= cfg.cum_min_edges and cumulative >= cfg.cum_drop_threshold:
            consumed.update(path)
            chains.append(
                DegradationChain(
                    super_ids=tuple(path),
                    run_ids=tuple(cond.super_nodes[s].exit_node for s in path),
                    scores=tuple(cond.super_nodes[s].score for s in path),
                    cumulative_drop=cumulative,
                )
            )
    return chains


def _analyze(cond: Condensation, inp: BlameInput) -> CutAnalysis:
    cfg = inp.config
    dag = cond.dag

    # A structural root is a payload-less orchestrator ENTRY point (a source
    # super-node whose exit node was left unscored with reason "payload_missing").
    # It has nothing upstream and provably contributed no CONTENT — so it can
    # neither be a culprit nor hide one. Treating it as "unknown upstream" is the
    # bug behind a directly-observed failure being reported at 21%: it both
    # "cannot be the origin" and "caps confidence as a hidden origin", which
    # cannot both be true.
    def _is_structural_root_sid(sid: int) -> bool:
        sn = cond.super_nodes[sid]
        ns = inp.scores.get(sn.exit_node)
        return (
            dag.in_degree(sid) == 0
            and ns is not None
            and ns.unscored_reason == "payload_missing"
        )

    def _only_structural_root_preds(sid: int) -> bool:
        preds = list(dag.predecessors(sid))
        return bool(preds) and all(_is_structural_root_sid(p) for p in preds)

    # Unknown upstream = a genuinely UNKNOWN scored-None ancestor that could hide
    # a culprit. Structural roots are excluded: their unscored-ness is by design,
    # not a blind spot, so they must NOT suppress confidence.
    def _unknown_upstream(sid: int) -> bool:
        return any(
            cond.super_nodes[a].score is None and not _is_structural_root_sid(a)
            for a in nx.ancestors(dag, sid)
        )
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
        # "source-like": a true source, OR a node whose ONLY predecessors are
        # structural roots. Both received a clean, observable handoff (the raw
        # request) — the roots contributed no content — so the honest baseline is
        # 1.0. This is what makes a first-node failure attribute to the node
        # itself (high confidence) instead of collapsing to "unknown upstream".
        source_like = is_source or _only_structural_root_preds(sid)
        # base for display/evidence: real predecessor, else the 1.0 source-like
        # baseline (assumed). The observed_drop check below never uses the
        # assumed baseline — only a real predecessor proves quality "broke here".
        if observed:
            base = max(known_pred_scores)
        elif source_like:
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
            unknown_upstream=_unknown_upstream(sid),
            is_source=is_source,
            iterations=sn.iterations,
            end_time=inp.node_end_times.get(sn.exit_node),
            observed_drop=observed_drop,
            recovered=recovered,
            base_assumed=not observed and source_like,
        )
        if observed_drop:
            drop_origins.append(cand)
        elif is_boundary and degraded and not recovered:
            propagating.append(cand)
        elif is_boundary and degraded:
            recovered_boundaries.append(cand)

    chains = _degradation_chains(cond, inp)

    # A recovered (transient-low) boundary is the culprit only when nothing else
    # explains the failure. A real downstream drop-origin always wins over it —
    # that is the "blame the node that broke, not the orchestrator" fix.
    origins = drop_origins + propagating
    if not origins and chains:
        # No single edge broke, but quality eroded past cum_drop_threshold over
        # consecutive steps. The origin is the first eroding node (the chain head
        # is the last healthy node); the drop is the CUMULATIVE decline against
        # the healthy head — giving up with "no significant drops" here would
        # discard a real, localisable signal.
        for ch in chains:
            first_sid = ch.super_ids[1]
            sn = cond.super_nodes[first_sid]
            if _input_flawed(inp, sn.exit_node):
                continue
            origins.append(
                Candidate(
                    super_id=first_sid,
                    run_id=sn.exit_node,
                    score=ch.scores[1],
                    base=ch.scores[0],
                    drop=ch.cumulative_drop,
                    unknown_upstream=_unknown_upstream(first_sid),
                    is_source=dag.in_degree(first_sid) == 0,
                    iterations=sn.iterations,
                    end_time=inp.node_end_times.get(sn.exit_node),
                    observed_drop=False,
                    cumulative_path=ch.run_ids,
                )
            )
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
    return CutAnalysis(
        tuple(final), tuple(unscored), had_significant_drop, tuple(chains)
    )


def select_candidates(inp: BlameInput) -> list[Candidate]:
    """Unshadowed cut-point origins in deterministic topological order."""
    return list(_analyze(condense(inp), inp).candidates)
