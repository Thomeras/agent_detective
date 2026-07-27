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

from dataclasses import dataclass, replace

import networkx as nx

from .condense import Condensation, _chron_key, condense
from .roles import is_verifier
from .types import BlameInput


@dataclass(frozen=True)
class Candidate:
    super_id: int
    run_id: str                 # exit node of the super-node
    # The node's judged quality score, or None when it was NEVER JUDGED. Only the
    # deterministic channel can produce such a candidate (a hard check localised
    # the fault without the judge), and the distinction is load-bearing: this used
    # to be a plain float and an unjudged deterministic origin was written in as
    # 0.0, which the report then rendered as "judged 0.00" — an assertion that the
    # judge scored the node terrible when in fact it never ran. Absence stays
    # absent; every consumer (confidence terms, candidacy, narrative) must branch
    # on None rather than arithmetic on a stand-in.
    score: float | None
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
    # Which evidence channel made this node an origin (spec: channel decoupling):
    #   "content"       — a score drop / degraded boundary (the classic lattice);
    #   "deterministic" — a hard check observed a fault here (contract rewrite:
    #                     input intact -> output nonconformant), INDEPENDENT of
    #                     the judged quality — so score is untouched, there is no
    #                     assumed baseline and no observability cap on it;
    #   "both"          — the same node is an origin in BOTH lattices, ONE origin
    #                     carrying two defects (not two origins, not dropped from
    #                     either lattice).
    # The content and deterministic lattices shadow SEPARATELY: content shadowing
    # (ancestry) may never bury a point-attributable deterministic origin.
    via: str = "content"
    # Direct predecessors whose score is UNKNOWN. "Quality was fine going in" is
    # then a claim about inputs one of which was never measured — the candidate
    # still stands (a single judge error must not blind the whole analysis) but
    # the candidacy trace has to say so instead of implying a clean handoff.
    unmeasured_inputs: tuple[str, ...] = ()
    # Set when the origin was localized INSIDE a multi-member SCC by
    # ``_scc_internal_origins``: the member whose own evidence qualified. The
    # condensation hides intra-SCC edges, so this is the only carrier of "which
    # member broke" — blame drills to THIS member rather than to the merely
    # worst-scoring one, which may be its victim.
    scc_member: str | None = None


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
            # Ordered by the EXIT NODE's chronological key, never by super-node
            # id: `nx.condensation` numbers components in the order it happens to
            # discover them, which follows the order edges were inserted. Two
            # successors with the SAME score then resolved to whichever id sorted
            # first, so the same run produced a different degradation chain — and
            # a different candidacy trace — depending on the order the exporter
            # emitted its spans.
            for succ in sorted(
                cond.dag.successors(cur),
                key=lambda s: _chron_key(inp, cond.super_nodes[s].exit_node),
            ):
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
        #
        # "Fine going in" means EVERY scored predecessor was healthy, not just
        # the best one. At a JOIN quality is bounded by the WORST input: a node
        # that faithfully merged one good and one broken branch would otherwise
        # measure a large drop against the good branch and be reported as the
        # place quality broke. Normally the broken branch is itself an origin and
        # shadows the join, but when it is EXCLUDED from candidacy — it processed
        # already-flawed input (root_cause_external), or its judge errored — the
        # shadow never comes and the merger takes the blame for an input it
        # never damaged.
        inputs_were_healthy = observed and min(known_pred_scores) >= cfg.threshold
        observed_drop = (
            inputs_were_healthy
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
        #
        # But the claim has to survive the score map. ``input_flawed`` is a JUDGE
        # verdict about an input the engine can also SEE: when every scored
        # predecessor came out healthy, the claim is contradicted by measurement
        # and may not silently clear the node — that is how a judge's stray
        # "my input was bad" (a documented confabulation of this very judge lane)
        # moved blame onto whatever merged its output downstream. At a real
        # boundary, or with a genuinely degraded predecessor, the exclusion stands.
        #
        # VERIFIERS are exempt from that override: for them "the input was
        # flawed" IS the verdict they were asked to issue, and their score
        # measures whether that verdict was RIGHT, not whether their own content
        # degraded. Reading it as a content drop would blame the whistle-blower.
        _flawed_claim_refuted = (
            observed
            and min(known_pred_scores) >= cfg.threshold
            and not is_verifier(inp.agent_names.get(sn.exit_node))
        )
        if _input_flawed(inp, sn.exit_node) and not _flawed_claim_refuted:
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
            unmeasured_inputs=tuple(
                sorted(
                    cond.super_nodes[p].exit_node
                    for p in dag.predecessors(sid)
                    if cond.super_nodes[p].score is None
                )
            ),
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

    # Intra-SCC origins: the condensation scores a super-node by its EXIT member,
    # so a cycle whose exit is healthy hides every broken member inside it. Run
    # the same origin criterion over the ORIGINAL graph inside each cycle that did
    # not already qualify as a whole.
    internal = _scc_internal_origins(
        cond, inp, dag, {c.super_id for c in origins}, _unknown_upstream
    )
    if any(c.observed_drop for c in internal):
        had_significant_drop = True
    origins = origins + internal
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

    # --- CONTENT channel: shadowing by ancestry ---------------------------
    # A downstream origin with an ancestor origin inherited its low quality; the
    # score alone cannot tell own-degradation from inherited, so ancestry is the
    # (necessary) proxy. This proxy is CONTENT-ONLY (see the deterministic channel
    # below, which needs no such proxy).
    origin_ids = {c.super_id for c in origins}
    content_final = [
        replace(c, via="content")
        for c in origins
        if not any(a in origin_ids for a in nx.ancestors(dag, c.super_id))
    ]

    # --- DETERMINISTIC channel: shadowing by EVIDENCE, not topology -------
    # A contract rewrite is point-attributable: the node's own input/output diff
    # OBSERVED the carried parameter arrive intact and leave rewritten, so it
    # originated here regardless of the content quality above it. Two independent
    # rewrites are two origins with no shadowing between them; there is never an
    # assumed baseline or an observability cap in this channel. (A second rewrite
    # of the SAME chain — input already arrived nonconformant — is a secondary
    # actor, not an origin; that basis refinement is deferred to the corpus.)
    det_final = _deterministic_origins(cond, inp, dag)

    # --- MERGE: one node in BOTH lattices is ONE origin, via="both" -------
    final = _merge_channels(content_final, det_final)
    return CutAnalysis(
        tuple(final), tuple(unscored), had_significant_drop, tuple(chains)
    )


def _scc_internal_origins(
    cond: Condensation,
    inp: BlameInput,
    dag,
    already: set[int],
    unknown_upstream,
) -> list[Candidate]:
    """Origins localized INSIDE a multi-member SCC (spec 3.3 blind spot).

    ``condense`` scores a super-node by its EXIT member — the value that flows
    downstream — and keeps ``min_member_score`` as evidence only. That is right
    for propagation and wrong for localization: when the exit is healthy the
    whole cycle reads healthy and a broken member inside it can never become a
    candidate. The shape this hits is not exotic — it is the ordinary
    orchestrator that delegates to sub-agents (``SPAWN`` out, ``TOOL_DELEGATION``
    back, a 2-cycle per pair) and, ending last, IS the exit node. The broken
    sub-agent then sits in the score map at 0.10 while the verdict blames a
    downstream node or the orchestration layer.

    The fix is to stop pretending intra-SCC edges do not exist: re-run the origin
    criterion on the ORIGINAL graph for every member of a cycle that did not
    already qualify as a whole (``already``), and hand the qualifying member to
    the drill via ``Candidate.scc_member``. Cycles that ARE already candidates —
    the poisoned peer mesh whose exit is itself degraded — are left untouched.

    The candidate carries no ``base``/``drop`` of its own: those numbers belong to
    the member, and attributing an intra-cycle drop to the exit node would be the
    same fabrication the assumed-baseline rules exist to prevent. Blame recomputes
    them for the drilled member.
    """
    cfg = inp.config
    out: list[Candidate] = []
    for sid in cond.topo:
        if sid in already:
            continue
        sn = cond.super_nodes[sid]
        if len(sn.members) < 2:
            continue
        best: tuple[float, str, bool, bool] | None = None  # score, member, observed, recovered
        for m in sn.members:
            ns = inp.scores.get(m)
            if ns is None or ns.score is None:
                continue
            # Same exclusion as the condensation-level lattice: a node that
            # faithfully processed already-flawed input is a propagation point.
            if ns.input_flawed is True:
                continue
            s = ns.score
            pred_scores = [
                inp.scores[p].score
                for p in cond.graph.predecessors(m)
                if inp.scores.get(p) is not None and inp.scores[p].score is not None
            ]
            succ_scores = [
                inp.scores[x].score
                for x in cond.graph.successors(m)
                if inp.scores.get(x) is not None
            ]
            # A later iteration that came out healthy is what a retry loop is
            # FOR. Recording recovery here is what keeps the fix from turning
            # every successful retry into a fresh cut_point: with an ok terminal
            # the projection reports it as the near-miss it is.
            member_recovered = bool(succ_scores) and all(
                x is not None and x >= cfg.threshold for x in succ_scores
            )
            if pred_scores:
                # Same criterion as the outer lattice, including the join rule:
                # every scored input must have been healthy for "quality was fine
                # going in" to hold.
                if min(pred_scores) < cfg.threshold:
                    continue
                drop = max(0.0, max(pred_scores) - s)
                if drop < cfg.gap_threshold or drop < cfg.min_drop:
                    continue
                observed = True
            else:
                # No scored predecessor anywhere: a degraded member at the
                # observability boundary. A recovered one is the weakest signal
                # there is (no break was observed, and it was cured) — the outer
                # lattice treats recovered boundaries as a last resort, so an
                # in-cycle one is simply not an origin.
                if s >= cfg.threshold or member_recovered:
                    continue
                observed = False
            if best is None or s < best[0]:
                best = (s, m, observed, member_recovered)
        if best is None:
            continue
        score, member, observed, member_recovered = best
        out.append(
            Candidate(
                super_id=sid,
                run_id=sn.exit_node,
                # The exit's score is what flows downstream and stays the
                # super-node's identity; the member's own numbers travel via
                # scc_member and are recomputed by the drill.
                score=sn.score if sn.score is not None else score,
                base=None,
                drop=None,
                unknown_upstream=unknown_upstream(sid),
                is_source=dag.in_degree(sid) == 0,
                iterations=sn.iterations,
                end_time=inp.node_end_times.get(sn.exit_node),
                observed_drop=observed,
                recovered=member_recovered,
                base_assumed=False,
                via="content",
                scc_member=member,
            )
        )
    return out


def _deterministic_defect(inp: BlameInput, run_id: str) -> bool:
    """A node's OWN output carries a hard, reproducible fault: a carried contract
    parameter it silently rewrote, or a fail-severity deterministic signal. This
    is observed, not inferred from a graded score — so it localises blame here
    independent of the judged quality."""
    ns = inp.scores.get(run_id)
    if ns is None:
        return False
    return bool(ns.contract_violations) or any(
        s.get("severity") == "fail" for s in ns.deterministic_signals
    )


def _has_fail_signal(inp: BlameInput, run_id: str) -> bool:
    ns = inp.scores.get(run_id)
    return ns is not None and any(
        s.get("severity") == "fail" for s in ns.deterministic_signals
    )


def _fresh_contract_origin(
    cond: Condensation, inp: BlameInput, dag, sid: int, run_id: str
) -> bool:
    """R2 basis: a contract violation ORIGINATES here only if the node did not
    merely carry a value an ancestor had already put into circulation on that key.
    Two ways it can be a secondary actor, and a JOIN needs both:

    - its INPUT on the key is an ancestor's rewritten value (docx->md upstream,
      then md->txt here): a second rewrite of the same chain, different basis;
    - its OUTPUT on the key is an ancestor's rewritten value: the nonconformant
      value already existed upstream, so this node echoed it rather than
      inventing it.

    The output-side test is what makes the rule survive a FAN-IN, and it is the
    refinement this comment used to defer "to the corpus". A joiner reading
    ``{"sections": [intro(markdown), specs(html), pricing(markdown)]}`` has a
    genuinely MULTI-VALUED input on ``format``; the scorer collapses that to the
    first scalar it meets (``markdown``), so the input-side test asks "did an
    ancestor rewrite TO markdown?", finds only the rewrite TO html, and declares
    the joiner a fresh origin — a second deterministic culprit for a fault it
    inherited from the branch it merged. Comparing the OUTPUT value instead needs
    no faith in the collapsed input: html was already in circulation upstream.

    Cost of the rule, stated plainly: a genuinely INDEPENDENT second rewrite to
    the same value on the same key is suppressed too (it reads as propagation of
    the first). Nothing in the graph distinguishes the two without payload-level
    dataflow, and the false positive — blaming the merger — is the one that
    misdirects the reader.

    Fresh iff ANY of this node's violations was neither fed by nor already
    produced by an ancestor's rewrite."""
    ns = inp.scores.get(run_id)
    if ns is None or not ns.contract_violations:
        return False
    ancestor_rewrites: set[tuple] = set()   # (key, value the ancestor rewrote TO)
    for anc in nx.ancestors(dag, sid):
        for m in cond.super_nodes[anc].members:
            mns = inp.scores.get(m)
            if mns is None:
                continue
            for k, _iv, ov in mns.contract_violations:
                ancestor_rewrites.add((k, ov))
    return any(
        (k, iv) not in ancestor_rewrites and (k, ov) not in ancestor_rewrites
        for k, iv, ov in ns.contract_violations
    )


def _deterministic_origins(
    cond: Condensation, inp: BlameInput, dag
) -> list[Candidate]:
    """Origins in the deterministic channel: nodes whose own output was hard-fault
    -flagged. No score gate (a judge-healthy node with a contract breach is still
    an origin), no assumed baseline, no topological shadowing between INDEPENDENT
    faults. The one topology-aware distinction is the R2 basis check: a contract
    rewrite that merely inherited an already-nonconformant input is a secondary
    actor on the same chain, not a fresh origin."""
    out: list[Candidate] = []
    # Nodes the R2 secondary-actor rule suppressed. R2 says "someone upstream
    # already put this value in circulation, so you are propagation, not origin"
    # — sound only while that someone is actually REACHABLE. They are not always:
    # this channel inspects a cycle's exit member only, and a node that processed
    # flawed input is excluded as a propagation point. Suppress every candidate
    # on those runs and the verdict goes silent with two hard breaches sitting in
    # evidence — trading a wrong name for no name. Held back and used below.
    secondary: list[tuple] = []
    for sid in cond.topo:
        sn = cond.super_nodes[sid]
        run_id = sn.exit_node
        if not _deterministic_defect(inp, run_id):
            continue
        # A node that faithfully processed already-flawed input is a propagation
        # point, never an origin — same rule as the content channel.
        if _input_flawed(inp, run_id):
            continue
        # Secondary actor (R2): its only fault is a contract rewrite whose input
        # already arrived nonconformant from an ancestor — a fail-severity signal
        # is always a fresh origin, but an inherited-only rewrite is propagation.
        if not (
            _has_fail_signal(inp, run_id)
            or _fresh_contract_origin(cond, inp, dag, sid, run_id)
        ):
            secondary.append((sid, run_id))
            continue
        s = cond.super_nodes[sid].score
        succ_scores = [cond.super_nodes[x].score for x in dag.successors(sid)]
        recovered = bool(succ_scores) and all(
            x is not None and x >= inp.config.threshold for x in succ_scores
        )
        out.append(
            Candidate(
                super_id=sid,
                run_id=run_id,
                # None stays None: this channel localises WITHOUT the judge, so a
                # node the judge never scored is a perfectly valid origin here.
                # Substituting 0.0 (as this did) manufactured a judged verdict out
                # of an unjudged node.
                score=s,
                base=None,             # deterministic channel: no assumed baseline
                drop=None,
                unknown_upstream=False,
                is_source=dag.in_degree(sid) == 0,
                iterations=sn.iterations,
                end_time=inp.node_end_times.get(run_id),
                observed_drop=False,
                recovered=recovered,
                base_assumed=False,
                via="deterministic",
            )
        )
    if out or not secondary:
        return out
    # Everything was suppressed as secondary, so the primary R2 pointed at is not
    # observable in this channel. Naming the reachable breach with
    # `unknown_upstream` set is the honest reading: the fault is real and located
    # HERE, and an earlier origin may exist that the trace does not show. Saying
    # nothing would report a clean run over recorded evidence of a breach.
    return [
        Candidate(
            super_id=sid,
            run_id=run_id,
            score=cond.super_nodes[sid].score,
            base=None,
            drop=None,
            unknown_upstream=True,
            is_source=dag.in_degree(sid) == 0,
            iterations=cond.super_nodes[sid].iterations,
            end_time=inp.node_end_times.get(run_id),
            observed_drop=False,
            recovered=False,
            base_assumed=False,
            via="deterministic",
        )
        for sid, run_id in secondary[:1]   # the earliest in topological order
    ]


def _merge_channels(
    content: list[Candidate], deterministic: list[Candidate]
) -> list[Candidate]:
    """Union the two lattices keyed by run_id. A node present in BOTH is ONE origin
    with via='both' (keeping the content candidate's score/drop/base for its
    content aspect); a node in only one keeps that channel's via. Deterministic
    order: content candidates first (topo), then deterministic-only ones."""
    by_content = {c.run_id: c for c in content}
    det_ids = {c.run_id for c in deterministic}
    merged: list[Candidate] = []
    for c in content:
        if c.run_id in det_ids:
            # The deterministic evidence sits on the EXIT node's own input/output
            # diff, so blame must stay there: drop the intra-SCC drill hint that
            # would otherwise move the culprit to a member with no hard evidence.
            merged.append(replace(c, via="both", scc_member=None))
        else:
            merged.append(c)
    for c in deterministic:
        if c.run_id not in by_content:
            merged.append(c)
    return merged


def select_candidates(inp: BlameInput) -> list[Candidate]:
    """Unshadowed cut-point origins in deterministic topological order."""
    return list(_analyze(condense(inp), inp).candidates)
