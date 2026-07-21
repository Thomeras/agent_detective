"""find_blame() orchestration and the first-match-wins classification table
(spec 3.7)."""

import networkx as nx

from .condense import _chron_key, condense
from .confidence import compute_confidence
from .cost import downstream_cost
from .cutpoint import _analyze
from .loops import _detect_anomalies
from .path import propagation_path
from .types import BlameInput, BlameReport, Evidence


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

    notes: list[str] = []
    culprits: list[str] = []
    confidence = 0.0
    candidate_sids = {c.super_id: c for c in candidates}
    anomalous_candidate_sids = {
        cond.node_to_super[a.member_run_ids[0]] for a in anomalies
    }

    if all(s is None for s in score_map.values()):
        # Row 1: all scores UNKNOWN.
        report_type = "unclassified"
        notes.append("no_scores: all scores unknown")
    elif (
        len(candidates) == 1
        and candidates[0].is_source
        and inp.scores.get(candidates[0].run_id) is not None
        and inp.scores[candidates[0].run_id].input_flawed is True
    ):
        # Row 2: single source candidate whose input was already flawed.
        candidate = candidates[0]
        report_type = "root_cause_external"
        culprits = [candidate.run_id]
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
        culprits = [candidate.run_id]
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
        and not unscored
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
        # Deterministic choice: all preconditions are hard evidence.
        confidence = 1.0
        notes.append(
            "composition_failure: all scores above threshold and no significant "
            f"drops, but terminal verdict is bad; blaming source '{culprits[0]}'"
        )
    else:
        # Row 7 fallback (composition_failure and loop rows handled elsewhere).
        report_type = "unclassified"
        notes.append(
            "unclassified: no cut-point candidates and composition-failure "
            "preconditions not met"
        )

    path = propagation_path(inp, cond, culprits[0]) if culprits else []
    cost = downstream_cost(inp, culprits)

    unknown_ancestors: set[str] = set()
    for c in culprits:
        if c in cond.graph:
            unknown_ancestors.update(
                n for n in nx.ancestors(cond.graph, c) if score_map.get(n) is None
            )

    evidence = Evidence(
        score_map=score_map,
        drops={c.run_id: c.drop for c in candidates if c.drop is not None},
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
