"""find_blame() orchestration and the first-match-wins classification table
(spec 3.7)."""

from dataclasses import replace

import networkx as nx

from .condense import _chron_key, condense
from .confidence import compute_confidence
from .cost import downstream_cost
from .cutpoint import Candidate, _analyze
from .loops import _detect_anomalies
from .path import propagation_path
from .types import BlameInput, BlameReport, Evidence


# Per-report-type confidence ceilings (spec: an honest detective never claims
# certainty it does not have). ``composition_failure`` is a fallback verdict —
# "we could not localise the fault, so we point at the orchestrator" — and must
# never be sold as a sure thing. Only ``cut_point`` (backed by a real score gap)
# and ``loop_detected`` (a deterministic limit breach) keep full confidence.
_CONFIDENCE_CAP: dict[str, float] = {
    "composition_failure": 0.4,
    "root_cause_external": 0.5,
    "multi_culprit": 0.8,
    "verification_gap": 0.6,
}

# Agent-name hints for verifier/gate nodes whose job is to catch bad work.
_VERIFIER_HINTS = ("qa", "eval", "review", "verif", "validat", "check", "critic", "audit", "gate")


def _is_verifier(name: str | None) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _VERIFIER_HINTS)


def _drill_into_loop(cond, inp, super_id, exit_node):
    """When the culprit super-node is a loop (multi-member SCC), the blame belongs
    to the worst-scoring MEMBER — where quality actually broke inside the loop —
    not the exit node that merely flows downstream. Returns (culprit_run_id,
    members, real_drop) where real_drop is the member's drop from its own raw
    (in-graph) predecessors, so it reflects the true break (e.g. act 0.93 ->
    render 0.27) rather than the loop's exit drop."""
    members = list(cond.super_nodes[super_id].members)
    scored = [
        (m, inp.scores[m].score)
        for m in members
        if inp.scores.get(m) is not None and inp.scores[m].score is not None
    ]
    if len(members) <= 1 or not scored:
        return exit_node, members, None
    culprit = min(scored, key=lambda ms: ms[1])[0]
    pred_scores = [
        inp.scores[p].score
        for p in cond.graph.predecessors(culprit)
        if inp.scores.get(p) is not None and inp.scores[p].score is not None
    ]
    real_drop = None
    if pred_scores:
        real_drop = max(0.0, max(pred_scores) - inp.scores[culprit].score)
    return culprit, members, real_drop


def _node_score(inp: BlameInput, run_id: str) -> float | None:
    ns = inp.scores.get(run_id)
    return ns.score if ns is not None else None


def find_blame(inp: BlameInput) -> BlameReport:
    cfg = inp.config
    cond = condense(inp)
    analysis = _analyze(cond, inp)
    candidates = list(analysis.candidates)
    anomalies = _detect_anomalies(cond, inp)

    graph_nodes = list(cond.graph.nodes)
    score_map = {n: _node_score(inp, n) for n in graph_nodes}
    unscored = sorted(n for n, s in score_map.items() if s is None)
    # Unscored nodes that could actually hide a culprit. A structural root — an
    # orchestrator "start" node deliberately left unscored (a source with no
    # output, unscored_reason "payload_missing") — has nothing upstream and hides
    # nothing, so it must NOT block composition_failure (that turned genuine
    # terminal failures into "unclassified" with $0 cost). A genuinely UNKNOWN
    # node (unscored_reason None / judge_error) still blocks, since it might be
    # the real culprit.
    _source_run_ids = {cond.super_nodes[s].exit_node for s in cond.sources}

    def _is_structural_root(n: str) -> bool:
        ns = inp.scores.get(n)
        return (
            n in _source_run_ids
            and ns is not None
            and ns.unscored_reason == "payload_missing"
        )

    hidden_unscored = [n for n in unscored if not _is_structural_root(n)]

    notes: list[str] = []
    culprits: list[str] = []
    confidence = 0.0
    loop_drops: dict[str, float] = {}  # real drop for culprits drilled inside a loop
    loop_members: list[str] = []       # members of the loop the culprit sits in
    candidate_sids = {c.super_id: c for c in candidates}
    anomalous_candidate_sids = {
        cond.node_to_super[a.member_run_ids[0]] for a in anomalies
    }

    # A condensation source whose own judge flagged its input as already flawed:
    # the fault entered from outside the observed graph. Detected directly (such
    # nodes are deliberately excluded from cut-point origins in cutpoint.py).
    flawed_sources = [
        cond.super_nodes[sid].exit_node
        for sid in cond.sources
        if (ns := inp.scores.get(cond.super_nodes[sid].exit_node)) is not None
        and ns.input_flawed is True
    ]

    if all(s is None for s in score_map.values()):
        # Row 1: all scores UNKNOWN.
        report_type = "unclassified"
        notes.append("no_scores: all scores unknown")
    elif not candidates and flawed_sources:
        # Row 2: no in-graph origin, but a source reports input_flawed=True.
        run_id = flawed_sources[0]
        sid = cond.node_to_super[run_id]
        score = score_map.get(run_id) or 0.0
        candidate = Candidate(
            super_id=sid,
            run_id=run_id,
            score=score,
            base=1.0,
            drop=max(0.0, 1.0 - score),
            unknown_upstream=False,
            is_source=True,
            iterations=cond.super_nodes[sid].iterations,
            end_time=inp.node_end_times.get(run_id),
        )
        report_type = "root_cause_external"
        culprits = [run_id]
        confidence = compute_confidence(candidate, cfg)
        notes.append(
            f"root_cause_external: source candidate '{candidate.run_id}' "
            "reports input_flawed=True"
        )
    elif anomalies and (
        not candidates or anomalous_candidate_sids & candidate_sids.keys()
    ):
        # Row 3: anomalous loop, and it is the culprit or there are no candidates.
        anomaly = next(
            (
                a
                for a in anomalies
                if cond.node_to_super[a.member_run_ids[0]] in candidate_sids
            ),
            anomalies[0],
        )
        report_type = "loop_detected"
        culprits = list(anomaly.member_run_ids)
        loop_candidate = candidate_sids.get(cond.node_to_super[anomaly.member_run_ids[0]])
        # No candidate: the deterministic limit breach itself is the evidence.
        confidence = compute_confidence(loop_candidate, cfg) if loop_candidate else 1.0
        notes.append(
            f"loop_detected: {anomaly.iterations} iterations "
            f"({anomaly.limit_kind}) of agent(s) {sorted(set(anomaly.agent_names))}"
        )
    elif len(candidates) == 1:
        # Row 4: exactly one unshadowed candidate.
        candidate = candidates[0]
        report_type = "cut_point"
        culprit, members, real_drop = _drill_into_loop(
            cond, inp, candidate.super_id, candidate.run_id
        )
        culprits = [culprit]
        if len(members) > 1 and real_drop is not None:
            loop_members = members
            # Blame drilled into a loop member; score/drop confidence off the
            # member's real break, not the loop-exit's.
            member_cand = replace(candidate, run_id=culprit,
                                   score=_node_score(inp, culprit) or candidate.score,
                                   drop=real_drop)
            confidence = compute_confidence(member_cand, cfg)
            loop_drops[culprit] = real_drop
            notes.append(
                f"cut_point: quality broke at '{culprit}' "
                f"(score={_node_score(inp, culprit):.3f}, drop={real_drop:.3f}) "
                f"inside a {len(members)}-member retry loop; the loop's exit "
                f"'{candidate.run_id}' only carried it downstream"
            )
        else:
            confidence = compute_confidence(candidate, cfg)
            notes.append(
                f"cut_point: single unshadowed candidate '{candidate.run_id}' "
                f"(score={candidate.score:.3f}, drop={candidate.drop})"
            )
    elif len(candidates) > 1:
        # Row 5: multiple independent candidates.
        report_type = "multi_culprit"
        culprits = [c.run_id for c in candidates]
        confidence = sum(
            compute_confidence(c, cfg, multi_culprit=True) for c in candidates
        ) / len(candidates)
        notes.append(
            f"multi_culprit: {len(candidates)} independent candidates: {culprits}"
        )
    elif (
        not candidates
        and inp.terminal_verdict is not None
        and inp.terminal_verdict.bad
        and not hidden_unscored
        and all(
            sn.score >= cfg.threshold
            for sn in cond.super_nodes.values()
            if sn.score is not None
        )
        and not analysis.had_significant_drop
        and cond.sources
    ):
        # Row 6: composition failure — everything looks healthy individually,
        # yet the terminal verdict is bad; blame the source/orchestrator.
        report_type = "composition_failure"
        source = min(cond.sources, key=lambda s: _chron_key(inp, cond.super_nodes[s].exit_node))
        culprits = [cond.super_nodes[source].exit_node]
        # Fallback verdict: no node individually broke, so we cannot localise the
        # fault. This is a *suspect* (likely an orchestration/design issue), not a
        # proven culprit — the cap below keeps the reported confidence honest.
        confidence = _CONFIDENCE_CAP["composition_failure"]
        notes.append(
            "composition_failure: no node individually failed (all scores above "
            "threshold, no significant drops) yet the terminal verdict is bad — "
            f"most likely an orchestration/design issue at source '{culprits[0]}'"
        )
    else:
        # Row 7 fallback (composition_failure and loop rows handled elsewhere).
        report_type = "unclassified"
        notes.append(
            "unclassified: no cut-point candidates and composition-failure "
            "preconditions not met"
        )

    # Verification gap: a verifier (qa/eval/review/…) whose VERDICT was wrong —
    # it passed bad work (or failed good work). With role-aware judging a verifier
    # is scored on the correctness of its PASS/FAIL, so a rubber-stamper lands
    # BELOW threshold while an honest whistle-blower stays high. Reading the
    # scored verdict (not the report's appearance) is what keeps this honest.
    terminal_bad = inp.terminal_verdict is not None and inp.terminal_verdict.bad
    verification_gaps: list[dict] = []
    for run_id in sorted(graph_nodes):
        ns = inp.scores.get(run_id)
        if (
            _is_verifier(inp.agent_names.get(run_id))
            and ns is not None
            and ns.score is not None
            and ns.score < cfg.threshold
        ):
            verification_gaps.append(
                {"run_id": run_id, "agent_name": inp.agent_names.get(run_id, "unknown")}
            )

    # When nothing localised as an origin but verifiers passed a bad terminal,
    # the rubber-stamping verifiers ARE the failure — retroactively blame them.
    if report_type in ("composition_failure", "unclassified") and verification_gaps:
        report_type = "verification_gap"
        culprits = [g["run_id"] for g in verification_gaps]
        confidence = _CONFIDENCE_CAP["verification_gap"]
        names = sorted({g["agent_name"] for g in verification_gaps})
        notes.append(
            f"verification_gap: verifier(s) {names} reported healthy while the "
            "terminal output is bad — they let bad work pass unflagged"
        )

    # Manifestation: terminal sinks where the failure surfaced — distinct from the
    # culprit(s) where it originated. "Where it broke" vs "where it showed".
    culprit_set = set(culprits)
    manifestation = [
        cond.super_nodes[sid].exit_node
        for sid in cond.dag.nodes
        if cond.dag.out_degree(sid) == 0
        and cond.super_nodes[sid].exit_node not in culprit_set
    ]

    # Honest-confidence ceiling per report type (cut_point / loop_detected keep
    # their computed value; fallback verdicts are capped so the UI never shows
    # "100% sure" on a guess).
    confidence = min(confidence, _CONFIDENCE_CAP.get(report_type, 1.0))

    path = propagation_path(inp, cond, culprits[0]) if culprits else []
    cost = downstream_cost(inp, culprits)

    unknown_ancestors: set[str] = set()
    for c in culprits:
        if c in cond.graph:
            unknown_ancestors.update(
                n for n in nx.ancestors(cond.graph, c) if score_map.get(n) is None
            )

    # Show every significant quality drop (> min_drop) against a node's best
    # scored predecessor, not only the candidates — the biggest drop in the graph
    # (e.g. a loop member) must never be missing from this signal.
    all_drops: dict[str, float] = {}
    for n in graph_nodes:
        s = score_map.get(n)
        if s is None:
            continue
        preds = [score_map[p] for p in cond.graph.predecessors(n) if score_map.get(p) is not None]
        if preds:
            d = max(preds) - s
            if d > 0.2:  # always surface a significant drop, even off a non-culprit
                all_drops[n] = d
    all_drops.update({c.run_id: c.drop for c in candidates if c.drop is not None})
    all_drops.update(loop_drops)

    # Candidacy trace: why each node was or wasn't blamed — so the verdict is
    # explainable rather than a black box (a verdict you cannot explain is one the
    # user will not trust).
    culprit_set = set(culprits)
    gap_ids = {g["run_id"] for g in verification_gaps}
    loop_set = set(loop_members)
    candidacy: dict[str, str] = {}
    for n in graph_nodes:
        s = score_map.get(n)
        if s is None:
            candidacy[n] = "unscored — never a culprit"
        elif n in culprit_set:
            candidacy[n] = "origin — quality broke here"
        elif n in gap_ids:
            candidacy[n] = "verification gap — passed bad work"
        elif n in loop_set:
            candidacy[n] = "loop member (same retry loop as the origin)"
        elif s < cfg.threshold:
            candidacy[n] = "below threshold — inherited, shadowed by the origin"
        elif all_drops.get(n, 0.0) > 0.2:
            candidacy[n] = f"dropped {all_drops[n]:.2f} but recovered / not the origin"
        else:
            candidacy[n] = "healthy"

    evidence = Evidence(
        score_map=score_map,
        drops=all_drops,
        judge_notes={
            n: inp.scores[n].judge_note
            for n in graph_nodes
            if inp.scores.get(n) is not None and inp.scores[n].judge_note is not None
        },
        error_span_ids=dict(inp.error_span_ids),
        loop_anomalies=anomalies,
        unknown_ancestors=sorted(unknown_ancestors),
        fact_propagation=None,
        notes=notes,
        manifestation_run_ids=manifestation,
        verification_gaps=verification_gaps,
        candidacy=candidacy,
    )
    return BlameReport(
        report_type=report_type,
        culprit_run_ids=culprits,
        propagation_path=path,
        confidence=confidence,
        evidence=evidence,
        downstream_cost_usd=cost,
        unscored_run_ids=unscored,
    )
