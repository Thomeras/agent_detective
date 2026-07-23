"""find_blame() orchestration and the first-match-wins classification table
(spec 3.7)."""

from dataclasses import replace

import networkx as nx

from .condense import _chron_key, condense
from .confidence import (
    _DETERMINISTIC_OBSERVATION,
    compute_confidence,
    compute_observation_confidence,
)
from .cost import downstream_cost
from .cutpoint import Candidate, _analyze
from .loops import _detect_anomalies
from .path import propagation_path
from .topology import classify_topology
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

# Structured scoring flags that assert a CONTENT defect (the judge admitted the
# node under-delivered). With a bad terminal corroborating them, the earliest
# flagged node marks where required content demonstrably went missing — the
# fabrication-cascade origin (everything downstream claimed success over it).
_CONTENT_FLAGS = frozenset(
    {"missing_required_content", "ignored_instruction", "factual_error"}
)


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


def _has_deterministic_defect(inp: BlameInput, run_id: str) -> bool:
    """A hard, reproducible signal that this node's output is defective — a
    contract violation or an admitted content flag — as opposed to a graded judge
    opinion. Drives observation_confidence to near-certain."""
    ns = inp.scores.get(run_id)
    if ns is None:
        return False
    return bool(ns.contract_violations) or bool(_CONTENT_FLAGS.intersection(ns.flags))


# Attribution ceiling for a CONTENT defect at the OBSERVABILITY BOUNDARY (its
# baseline is assumed, not measured). "The fault originated here" cannot be
# near-certain about a node whose predecessor was never scored — such a node is
# the origin partly because it is the first thing we could see. The cap is
# specific to the content defect: a contract violation is exempt entirely,
# because its input/output diff OBSERVED the carried parameter arriving intact
# and leaving rewritten — origination is observed, not inferred.
_BOUNDARY_ATTRIBUTION_CAP = 0.6


def _attribution_breakdown(
    inp: BlameInput, run_id: str, candidate, raw_attribution: float
) -> list[dict]:
    """Per-defect attribution: the same origin can carry defects with very
    different evidential strength, and one blended number takes the worse.

    - contract_violation: the check observed the parameter INTACT in the input
      and REWRITTEN in the output — origination is observed, not inferred →
      near-certain (0.95, the deterministic-observation convention).
    - content/score defect: the classic formula, hard-capped at the
      observability boundary (assumed baseline ⇒ ≤ 0.6) — no contract
      exception here; the exception belongs to the contract entry above.
    """
    ns = inp.scores.get(run_id)
    breakdown: list[dict] = []
    if ns is not None and ns.contract_violations:
        detail = "; ".join(f"{k}: {a!r}->{b!r}" for k, a, b in ns.contract_violations)
        breakdown.append(
            {
                "defect": "contract_violation",
                "attribution": _DETERMINISTIC_OBSERVATION,
                "basis": (
                    "deterministic: the carried parameter was observed intact "
                    f"in the input and rewritten in the output ({detail}) — "
                    "origination is observed, not inferred"
                ),
            }
        )
    content_attr = (
        min(raw_attribution, _BOUNDARY_ATTRIBUTION_CAP)
        if candidate.base_assumed
        else raw_attribution
    )
    breakdown.append(
        {
            "defect": "content_degradation",
            "attribution": content_attr,
            "basis": (
                "observability boundary — no scored predecessor, the baseline "
                f"is assumed (capped at {_BOUNDARY_ATTRIBUTION_CAP:.2f})"
                if candidate.base_assumed
                else "measured drop from a scored predecessor"
            ),
        }
    )
    return breakdown


def _verdict_attribution(
    inp: BlameInput, run_id: str, candidate, attribution: float, notes: list[str]
) -> float:
    """Headline attribution = attribution of the defect that CARRIES the verdict.

    Never a blend and never a ceiling that matches no defect: with a contract
    violation the verdict rests on observed origination (the input/output diff
    saw the parameter arrive intact), so the headline is the deterministic 0.95
    and the observability-boundary cap does not apply. Without contract evidence
    the verdict rests on the content defect, whose attribution IS capped at an
    assumed baseline — and the note says the cap is scoped to that defect.
    """
    ns = inp.scores.get(run_id)
    if ns is not None and ns.contract_violations:
        return _DETERMINISTIC_OBSERVATION
    if candidate.base_assumed and attribution > _BOUNDARY_ATTRIBUTION_CAP:
        notes.append(
            "attribution_capped: content_degradation — the origin sits at the "
            "observability boundary (no scored predecessor; the baseline is "
            "assumed, not measured), so attribution of the content defect "
            f"cannot exceed {_BOUNDARY_ATTRIBUTION_CAP:.2f}. The cap is "
            "specific to inferred defects; a deterministically observed defect "
            "(contract violation) is not subject to it"
        )
        return _BOUNDARY_ATTRIBUTION_CAP
    return attribution


def find_blame(inp: BlameInput) -> BlameReport:
    cfg = inp.config
    cond = condense(inp)
    analysis = _analyze(cond, inp)
    candidates = list(analysis.candidates)
    anomalies = _detect_anomalies(cond, inp)
    # Advisory topology classification (presentational; never drives verdicts).
    topology = classify_topology(inp.nodes, inp.edges)

    # Raw nodes in deterministic topological order (super-node topo order, then
    # chronological within each SCC). Everything keyed per-node downstream —
    # score map, candidacy — inherits this order, and the UI renders it as-is.
    graph_nodes: list[str] = []
    for _sid in cond.topo:
        graph_nodes.extend(cond.super_nodes[_sid].members)
    score_map = {n: _node_score(inp, n) for n in graph_nodes}

    # The tier1 terminal verdict is load-bearing evidence for several report
    # types; a report that leans on "terminal is bad" must SHOW that evidence.
    tv = inp.terminal_verdict
    # A verdict is trustworthy ground truth ONLY if the judge could see the
    # deliverable. A not-checkable verdict (content absent from the payload) is
    # never allowed to drive a "bad terminal" conclusion — otherwise a healthy
    # run whose artifact simply wasn't embedded gets a fabricated failure and a
    # cascade pinned on an innocent node.
    terminal_checkable = tv is None or tv.checkable
    terminal_bad = tv is not None and tv.bad and tv.checkable
    # Terminal ground truth says the deliverable is GOOD: not bad AND actually
    # checkable (the judge saw the artifact). This is the ONLY signal that can
    # prove a verifier's FAIL was a false alarm — and its mere existence is what
    # refutes a "wrong PASS" gap that the unreliable role-aware score tried to
    # invent (a good deliverable cannot have been passed through wrongly).
    terminal_ok = tv is not None and not tv.bad and tv.checkable
    terminal_evidence = (
        {"bad": tv.bad, "score": tv.score, "reasoning": tv.reasoning,
         "checkable": tv.checkable, "stale": tv.stale}
        if tv is not None
        else None
    )
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
    # Split confidence (see Evidence): observation = "is the output defective?"
    # (certain for a hard signal), attribution = "did the fault originate here?".
    # Set for the localised report types below; None where the headline
    # confidence carries its own capped semantics (composition/verification/…).
    observation_confidence: float | None = None
    attribution_confidence: float | None = None
    attribution_breakdown: list[dict] = []
    fabrication_origin: str | None = None  # set by the fabrication-cascade row
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
        members_all = list(cond.super_nodes[candidate.super_id].members)
        # DEGRADED-BUT-RECOVERED: the node underperformed, but every successor
        # recovered AND the terminal deliverable is ok (checkable ground truth).
        # That is a near-miss — a fragile node the pipeline compensated for — not
        # a live quality break. Reporting it as a cut_point "where quality broke"
        # is a false alarm on a run that turned out fine (alert fatigue). It is
        # still worth surfacing: this node may not be lucky next time.
        if candidate.recovered and terminal_ok and len(members_all) <= 1:
            report_type = "degraded_recovered"
            culprit = candidate.run_id
            culprits = [culprit]
            _raw_attr = compute_confidence(candidate, cfg)
            attribution_confidence = _verdict_attribution(
                inp, culprit, candidate, _raw_attr, notes
            )
            attribution_breakdown = _attribution_breakdown(
                inp, culprit, candidate, _raw_attr
            )
            observation_confidence = compute_observation_confidence(
                candidate, cfg, deterministic=_has_deterministic_defect(inp, culprit)
            )
            # Headline is the OBSERVATION: we are confident the node underperformed
            # (that is the signal). Attribution is shown alongside but the story is
            # "degraded here, recovered downstream", not "this is the culprit".
            confidence = observation_confidence
            notes.append(
                f"degraded_recovered: '{inp.agent_names.get(culprit, culprit)}' "
                f"scored {candidate.score:.2f} (below threshold {cfg.threshold:.2f})"
                + (
                    f" and silently violated an input contract "
                    f"({'; '.join(f'{k}:{a!r}->{b!r}' for k, a, b in inp.scores[culprit].contract_violations)})"
                    if inp.scores.get(culprit) and inp.scores[culprit].contract_violations
                    else ""
                )
                + f", but every successor scored healthy and the terminal "
                f"deliverable is ok (checkable ground truth: {tv.reasoning!r}) — a "
                "near-miss the pipeline compensated for, not a live quality break. "
                "Surfaced as a fragile point to harden, not paged as a broken run"
                + (
                    ". CAVEAT: recovery is proven for CONTENT only — the silently "
                    "rewritten contract parameter leaves the run unverified in "
                    "contract (see contract_vs_terminal); do not treat it as fully "
                    "clean"
                    if inp.scores.get(culprit) and inp.scores[culprit].contract_violations
                    else ""
                )
            )
        else:
            report_type = "cut_point"
            culprit, members, real_drop = _drill_into_loop(
                cond, inp, candidate.super_id, candidate.run_id
            )
            culprits = [culprit]
            conf_candidate = candidate
            if len(members) > 1 and real_drop is not None:
                loop_members = members
                # Blame drilled into a loop member; score/drop confidence off the
                # member's real break, not the loop-exit's.
                member_cand = replace(candidate, run_id=culprit,
                                       score=_node_score(inp, culprit) or candidate.score,
                                       drop=real_drop)
                conf_candidate = member_cand
                loop_drops[culprit] = real_drop
                notes.append(
                    f"cut_point: quality broke at '{culprit}' "
                    f"(score={_node_score(inp, culprit):.3f}, drop={real_drop:.3f}) "
                    f"inside a {len(members)}-member retry loop; the loop's exit "
                    f"'{candidate.run_id}' only carried it downstream"
                )
            elif candidate.cumulative_path:
                chain = " -> ".join(
                    f"{inp.agent_names.get(r, r)}({_node_score(inp, r):.2f})"
                    for r in candidate.cumulative_path
                )
                notes.append(
                    f"cut_point (cumulative degradation): no single step crossed the "
                    f"gap threshold ({cfg.gap_threshold:.2f}), but quality eroded by "
                    f"{candidate.drop:.2f} across {chain} — past the cumulative "
                    f"threshold ({cfg.cum_drop_threshold:.2f}). The erosion starts at "
                    f"'{candidate.run_id}' (score {candidate.score:.2f} from healthy "
                    f"base {candidate.base:.2f}); review the whole chain, the seed of "
                    f"the failure may sit in the last healthy node's output"
                )
            elif candidate.base_assumed:
                notes.append(
                    f"cut_point: single unshadowed candidate '{candidate.run_id}' "
                    f"(score={candidate.score:.3f}; no scored predecessor — the "
                    "1.00 baseline is ASSUMED from a clean handoff, not measured)"
                )
            else:
                notes.append(
                    f"cut_point: single unshadowed candidate '{candidate.run_id}' "
                    f"(score={candidate.score:.3f}, drop={candidate.drop})"
                )
            # Split confidence: headline is ATTRIBUTION (honest about whether the
            # fault originated here), observation is shown alongside so a certain
            # defect at the observable boundary is never buried by localisation
            # doubt. An assumed-baseline origin cannot claim near-certain
            # attribution — it is the origin partly because it is the first node
            # we could see (structural cap, stated in the notes).
            _raw_attr = compute_confidence(conf_candidate, cfg)
            attribution_confidence = _verdict_attribution(
                inp, culprit, conf_candidate, _raw_attr, notes
            )
            attribution_breakdown = _attribution_breakdown(
                inp, culprit, conf_candidate, _raw_attr
            )
            observation_confidence = compute_observation_confidence(
                conf_candidate, cfg, deterministic=_has_deterministic_defect(inp, culprit)
            )
            confidence = attribution_confidence
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
    elif not candidates and terminal_bad and (
        content_flagged := [
            n
            for n in graph_nodes
            if (fns := inp.scores.get(n)) is not None
            and fns.score is not None
            and fns.input_flawed is not True
            and not _is_verifier(inp.agent_names.get(n))
            and _CONTENT_FLAGS.intersection(fns.flags)
        ]
    ):
        # Row 5b: fabrication cascade (claims-vs-reality divergence). No score
        # gap localised an origin, but a producer's OWN judge admitted a content
        # defect via a structured flag, and the bad terminal verdict corroborates
        # it: the required content demonstrably went missing at the earliest
        # flagged node, while everything downstream reported success anyway. The
        # honest self-critical node is the origin; the confident downstream
        # claims are the cascade.
        report_type = "cut_point"
        fabrication_origin = content_flagged[0]
        culprits = [fabrication_origin]
        # Indirect but corroborated evidence (flag + terminal ground truth):
        # stronger than the composition guess (0.4), weaker than a hard score gap.
        confidence = 0.65
        f_ns = inp.scores[fabrication_origin]
        f_flags = ", ".join(sorted(_CONTENT_FLAGS.intersection(f_ns.flags)))
        others = [
            inp.agent_names.get(n, n) for n in content_flagged[1:]
        ]
        notes.append(
            "cut_point (fabrication cascade): no score gap, but "
            f"'{inp.agent_names.get(fabrication_origin, fabrication_origin)}' was "
            f"flagged [{f_flags}] by its own judge and the bad terminal verdict "
            f"corroborates it — terminal evidence: {tv.reasoning!r} (tier1 "
            f"terminal judge, score={tv.score}). Required content went missing "
            "here first; downstream nodes claimed success over it"
            + (f" (also flagged: {others})" if others else "")
        )
    elif (
        not candidates
        # terminal_bad (checkable-gated), NOT the raw flag: a stale or
        # not-checkable "bad" is not ground truth and must not manufacture an
        # orchestration suspect. (The worker used to mask this by zeroing
        # `bad` for not_checkable rows; the stale path keeps bad=True with
        # checkable=False and exposed the raw check.)
        and terminal_bad
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
            "threshold, no significant single-edge drops, no cumulative "
            "degradation chain) yet the terminal verdict is bad — terminal "
            f"evidence: {tv.reasoning!r} (tier1 terminal judge, score={tv.score}). "
            "Most likely an orchestration/task-design issue entering at source "
            f"'{culprits[0]}'"
        )
    else:
        # Row 7 fallback. A negative conclusion needs a trace as much as a
        # positive one: state exactly WHICH precondition ruled each verdict out,
        # so "correctly rejected" is distinguishable from "never ran".
        report_type = "unclassified"
        reasons: list[str] = []
        if tv is None:
            reasons.append(
                "no terminal verdict available (tier1 terminal judge missing or "
                "errored) — composition_failure and fabrication-cascade both "
                "require terminal ground truth"
            )
        elif not tv.checkable:
            reasons.append(
                "terminal verdict not checkable (the judge never saw the "
                "deliverable) — discarded as ground truth, so it cannot support "
                "composition_failure or fabrication-cascade"
            )
        elif not tv.bad:
            reasons.append(
                f"terminal verdict is ok (score={tv.score}) — there is no "
                "terminal failure for a fallback verdict to explain"
            )
        if hidden_unscored:
            names = sorted(inp.agent_names.get(n, n) for n in hidden_unscored)
            reasons.append(
                f"genuinely unscored node(s) {names} could hide the culprit — "
                "blocks composition_failure"
            )
        if analysis.had_significant_drop:
            reasons.append(
                "a significant drop was observed but every origin was shadowed "
                "or excluded"
            )
        unhealthy = sorted(
            inp.agent_names.get(n, n)
            for n in graph_nodes
            if (u := score_map.get(n)) is not None and u < cfg.threshold
        )
        if unhealthy:
            reasons.append(
                f"below-threshold node(s) {unhealthy} did not qualify as an "
                "origin (inherited/recovered degradation)"
            )
        if not reasons:
            reasons.append(
                "all scored nodes healthy and no failure signal to explain "
                "(e.g. a sampled healthy graph)"
            )
        notes.append("unclassified: no origin localised — " + "; ".join(reasons))

    # Honesty: a terminal verdict whose deliverable the judge could not see is
    # not evidence of anything. Say so plainly instead of letting a fabricated
    # "bad terminal" silently vanish or, worse, drive a culprit. A STALE verdict
    # gets its own narrative — the problem is not instrumentation, it is that
    # the verdict's deterministic basis no longer reproduces.
    if tv is not None and not terminal_checkable:
        if tv.stale:
            notes.append(
                "terminal_stale: the terminal verdict's deterministic basis no "
                "longer reproduces on the current payload/rule set — the tier1 "
                "verdict was computed under a different registered rule set, or "
                "the artifact/payload diverged (representation divergence). Its "
                f"verdict (bad={tv.bad}, score={tv.score}, {tv.reasoning!r}) is "
                "treated as UNRELIABLE, not ground truth. Re-run the analysis "
                "end-to-end (tier1 included) for a fresh verdict"
            )
        else:
            notes.append(
                "terminal_not_checkable: the terminal judge could not see the final "
                "deliverable (its content was absent from the payload — a file "
                "reference, an orchestrator wrapper, or a verifier verdict rather "
                "than the artifact), so there is NO terminal ground truth. Its "
                f"verdict (bad={tv.bad}, score={tv.score}, {tv.reasoning!r}) is "
                "discarded — not treated as a failure. Fix the instrumentation to "
                "embed the artifact text if you want a terminal check here"
            )

    # Instrumentation health: a NON-root node without an output payload cannot
    # be scored or blamed — that is a data-quality defect in the exporter and
    # must surface as a visible warning, not a footnote. (A structural root is
    # intentionally payload-less and is not warned about.)
    missing_payload = sorted(
        inp.agent_names.get(n, n)
        for n in graph_nodes
        if (mns := inp.scores.get(n)) is not None
        and mns.unscored_reason == "payload_missing"
        and not _is_structural_root(n)
    )
    if missing_payload:
        notes.append(
            f"instrumentation_warning: node(s) {missing_payload} have no output "
            "payload — they cannot be scored or blamed, which blinds the "
            "analysis; fix the exporter/instrumentation for these nodes"
        )

    # Topology-driven instrumentation-quality warning (same family as
    # payload_missing). The ONLY behavioral use of the advisory classification:
    # a disconnected graph means edges between components were never recorded.
    if topology["primary"] == "disconnected":
        notes.append(
            f"topology: graph has {topology['components']} weakly-connected "
            "components — runs share membership but lack instrumented edges "
            "between components; blame localisation across components is "
            "impossible. Enable A2A detection or instrument SPAWN/TOOL edges"
        )

    # Score/verdict conflict (honesty check): the tier1 terminal judge says the
    # final output is bad, yet a terminal-sink node scored healthy. Both cannot
    # be right. The terminal verdict is treated as ground truth for gap analysis
    # below, and the conflict is stated openly instead of being papered over.
    if terminal_bad:
        for _sid in cond.sinks:
            _sn = cond.super_nodes[_sid]
            if _sn.score is not None and _sn.score >= cfg.threshold:
                notes.append(
                    f"verdict_conflict: terminal verdict is bad (tier1 judge: "
                    f"{tv.reasoning!r}) yet terminal node "
                    f"'{inp.agent_names.get(_sn.exit_node, _sn.exit_node)}' scored "
                    f"{_sn.score:.2f} — treating the terminal verdict as ground "
                    "truth; the healthy score of a verifier that passed bad work "
                    "is itself part of the failure (see verification gaps)"
                )

    # Verification gap: a verifier (qa/eval/review/…) whose PASS was wrong.
    # Two independent detection routes:
    #  - "verdict_scored_incorrect": the role-aware judge scored the verifier's
    #    PASS/FAIL itself as wrong (score below threshold). This route is only as
    #    reliable as that one gpt-4o-mini judge — which we have caught scoring a
    #    verifier's PASS 0.27 ("your PASS was wrong") while a healthy run rendered
    #    a genuinely good deliverable, hallucinating that "the artifact is not
    #    visible" even though it was embedded in the payload. So a low role-aware
    #    score alone is NOT allowed to open a gap: it must be CROSS-CHECKED against
    #    terminal ground truth (see below). The terminal judge saw the deliverable;
    #    when it says the work is good, a "wrong PASS" claim is simply refuted, and
    #    when it says nothing (not_checkable / absent) there is no ground truth to
    #    manufacture a gap from at all.
    #  - "passed_bad_terminal": deduced — the terminal output is bad, so every
    #    verifier that let the work through with a healthy score issued a wrong
    #    PASS by definition, no matter how well its verdict *read*. Without this
    #    route the engine goes blind exactly when the judges do (e.g. verifiers
    #    "verifying" a file artifact nobody actually opened).
    verification_gaps: list[dict] = []
    scored_gap_ids: set[str] = set()
    for run_id in sorted(graph_nodes):
        ns = inp.scores.get(run_id)
        if not (
            _is_verifier(inp.agent_names.get(run_id))
            and ns is not None
            and ns.score is not None
            and ns.score < cfg.threshold
        ):
            continue
        # Cross-check the low role-aware score against terminal GROUND TRUTH.
        # judge_verifier.md always emits exactly one of issued_pass/issued_fail;
        # a verifier that did NOT issue FAIL let the work through (wrong-PASS
        # shape), one that issued FAIL raised an alarm (wrong-FAIL shape).
        #  - wrong PASS: a gap ONLY if the terminal corroborates badness
        #    (terminal_bad). A good/absent/not_checkable terminal leaves the
        #    "wrong PASS" resting on the unreliable role-aware score alone — the
        #    exact false positive (PASS + terminal ok => NOT a gap).
        #  - wrong FAIL (false alarm): a gap ONLY if the terminal is ok/good, i.e.
        #    ground truth confirms the failed work was actually fine. With a bad
        #    or not_checkable terminal there is no ground truth that the FAIL was
        #    wrong, so we do not manufacture a wrong-FAIL gap either.
        issued_fail = "issued_fail" in ns.flags
        corroborated = terminal_ok if issued_fail else terminal_bad
        if not corroborated:
            continue
        verification_gaps.append(
            {
                "run_id": run_id,
                "agent_name": inp.agent_names.get(run_id, "unknown"),
                "basis": "verdict_scored_incorrect",
            }
        )
        scored_gap_ids.add(run_id)

    if terminal_bad:
        # Only verifiers that saw the (eventually bad) work: with a localised
        # culprit that means verifiers downstream of it; with no culprit every
        # passing verifier is implicated.
        downstream_of_culprits: set[str] = set()
        for c in culprits:
            if c in cond.graph:
                downstream_of_culprits |= nx.descendants(cond.graph, c)
        for run_id in sorted(graph_nodes):
            ns = inp.scores.get(run_id)
            if (
                run_id not in scored_gap_ids
                and _is_verifier(inp.agent_names.get(run_id))
                and ns is not None
                and ns.score is not None
                and ns.score >= cfg.threshold
                # A verifier that issued FAIL blew the whistle — it did NOT let
                # the work through; only pass-issuers are retroactive gaps.
                and "issued_fail" not in ns.flags
                and (not downstream_of_culprits or run_id in downstream_of_culprits)
            ):
                verification_gaps.append(
                    {
                        "run_id": run_id,
                        "agent_name": inp.agent_names.get(run_id, "unknown"),
                        "basis": "passed_bad_terminal",
                    }
                )

    # Ground-truth score override: a role-aware verifier score CLAIMS to be
    # "verdict correctness". A verifier whose PASS is refuted by the terminal
    # verdict has that number disproved — the effective correctness of a rubber
    # stamp is ~0.1. Shown alongside the original, never silently rewritten;
    # without this the score map's "1.00" visually clears the guilty verifier.
    score_overrides = [
        {
            "run_id": g["run_id"],
            "original": score_map.get(g["run_id"]),
            "effective": 0.1,
            "reason": "PASS refuted by terminal ground truth — the judged "
            "'verdict correctness' cannot stand (rubber stamp)",
        }
        for g in verification_gaps
        if g.get("basis") == "passed_bad_terminal"
        and score_map.get(g["run_id"]) is not None
    ]
    # PRODUCERS refuted by a deterministic check get the same claimed→effective
    # treatment: the worker records the pre-override judged composite when a
    # contract/deterministic override lowered the score. Without this, the score
    # map shows a bare 0.10 while the judge note below still praises the work —
    # the refutation would be invisible exactly where the cascade shows.
    for n in graph_nodes:
        ns = inp.scores.get(n)
        if ns is None or ns.score is None:
            continue
        pre = ns.components.get("pre_override_composite")
        if pre is None or any(o["run_id"] == n for o in score_overrides):
            continue
        refuting = sorted(
            set(ns.flags)
            & (
                {"artifact_integrity_fail", "missing_required_section",
                 "numeric_invariant_breach", "language_mismatch",
                 "duplicate_side_effect", "tool_args_invalid"}
            )
        )
        if ns.contract_violations:
            refuting.append("contract_violation")
        if not refuting:
            refuting = ["deterministic override"]
        score_overrides.append(
            {
                "run_id": n,
                "original": pre,
                "effective": ns.score,
                "reason": (
                    "judged score refuted by deterministic check(s): "
                    + ", ".join(refuting)
                ),
            }
        )

    # When nothing localised as an origin but verifiers issued a wrong verdict,
    # the rubber-stamping (or false-alarming) verifiers ARE the failure —
    # retroactively blame them. The note must state each gap's ACTUAL basis and
    # only invoke "the terminal output is bad" when the terminal genuinely is bad
    # AND checkable: a verdict_scored_incorrect gap rests on the role-aware judge
    # scoring the verifier's own PASS/FAIL wrong, NOT on the terminal. Asserting a
    # bad terminal here — worse, while quoting an OK verdict's reasoning — is the
    # exact dishonesty this fixes (a false verification_gap on a healthy run).
    if report_type in ("composition_failure", "unclassified") and verification_gaps:
        report_type = "verification_gap"
        culprits = [g["run_id"] for g in verification_gaps]
        confidence = _CONFIDENCE_CAP["verification_gap"]
        parts: list[str] = []
        for g in verification_gaps:
            rid, name = g["run_id"], g["agent_name"]
            s = score_map.get(rid)
            gflags = inp.scores[rid].flags if inp.scores.get(rid) is not None else ()
            if g["basis"] == "passed_bad_terminal":
                parts.append(
                    f"'{name}' scored healthy ({s:.2f}) yet let the work through "
                    "while the terminal output is bad"
                )
            elif "issued_fail" in gflags:
                parts.append(
                    f"'{name}' issued a FAIL the role-aware judge scored wrong "
                    f"(score {s:.2f} < threshold {cfg.threshold:.2f}) — a false "
                    "alarm the ok terminal contradicts"
                )
            else:
                parts.append(
                    f"'{name}' issued a PASS the role-aware judge scored wrong "
                    f"(score {s:.2f} < threshold {cfg.threshold:.2f})"
                )
        note = "verification_gap: " + "; ".join(parts) + "."
        # Only quote the terminal as ground truth when it genuinely is one.
        if terminal_bad:
            note += (
                f" Terminal evidence (bad, ground truth): {tv.reasoning!r} "
                "(tier1 terminal judge)."
            )
        elif terminal_ok:
            note += (
                f" The terminal verdict is ok (score={tv.score}) — these are "
                "wrong-FAIL false alarms confirmed by ground truth, not "
                "passed-through bad work."
            )
        notes.append(note)

    # Manifestation: where the failure SURFACED — the terminal artifact/output.
    # A verifier sink (qa/eval) did not manifest anything; it issued a verdict
    # about work produced upstream, so verifier sinks map back to their nearest
    # non-verifier ancestor (the artifact producer). The producer may coincide
    # with the culprit — "broke where it showed" is a legitimate answer.
    # Manifestation only means something when there IS a failure that surfaced.
    # Suppress it when ground truth says the deliverable is fine (terminal_ok),
    # the run recovered (degraded_recovered), or the terminal verdict is STALE
    # (its failure claim was discarded as non-reproducible — with no live
    # failure evidence, "failure surfaced in output of X" is unsupported). A
    # localised failure with an absent terminal still surfaces normally.
    culprit_set = set(culprits)
    manifestation: list[str] = []
    terminal_stale = tv is not None and tv.stale
    if not terminal_ok and not terminal_stale and report_type != "degraded_recovered":
        for sid in cond.sinks:
            node = cond.super_nodes[sid].exit_node
            level, seen = [sid], {sid}
            while _is_verifier(inp.agent_names.get(node)):
                preds = sorted({p for s in level for p in cond.dag.predecessors(s)} - seen)
                if not preds:
                    break  # nothing but verifiers upstream: keep the sink itself
                seen.update(preds)
                producers = [
                    cond.super_nodes[p].exit_node
                    for p in preds
                    if not _is_verifier(
                        inp.agent_names.get(cond.super_nodes[p].exit_node)
                    )
                ]
                if producers:
                    node = max(
                        producers, key=lambda r: (inp.node_end_times.get(r, 0.0), r)
                    )
                    break
                level = preds
            if node not in manifestation:
                manifestation.append(node)

    # Claims-vs-reality conflict: the artifact PRODUCER scored healthy ("no
    # notable issues") while the terminal judge found its artifact deficient.
    # Two judges, same artifact, opposite verdicts — that must be confronted as
    # evidence, not left standing side by side. The terminal verdict is ground
    # truth, so the producer's healthy score is demoted to a suspect claim.
    reality_conflicts: set[str] = set()
    if terminal_bad:
        for m in manifestation:
            ms = score_map.get(m)
            if m not in culprit_set and ms is not None and ms >= cfg.threshold:
                reality_conflicts.add(m)
                notes.append(
                    f"claims_vs_reality: producer "
                    f"'{inp.agent_names.get(m, m)}' scored {ms:.2f} ('healthy') "
                    "for the very artifact the terminal judge rejected — "
                    f"terminal evidence: {tv.reasoning!r}. The node-level score "
                    "is overridden as a claim, not accepted as fact"
                )

    # Cascade participants: healthy-scoring PRODUCERS downstream of a
    # fabrication-cascade origin. Their "success" was built on input that
    # demonstrably lacked required content — those scores are unverified claims,
    # and labelling them plain "healthy" would hide the accomplices.
    cascade_participants: list[str] = []
    if fabrication_origin is not None and fabrication_origin in cond.graph:
        descendants = nx.descendants(cond.graph, fabrication_origin)
        cascade_participants = [
            n
            for n in graph_nodes
            if n in descendants
            and not _is_verifier(inp.agent_names.get(n))
            and (cs := score_map.get(n)) is not None
            and cs >= cfg.threshold
        ]
        if cascade_participants:
            names = [inp.agent_names.get(n, n) for n in cascade_participants]
            notes.append(
                f"cascade_participants: producer(s) {names} scored healthy "
                "while building on input flagged for missing required content — "
                "their success claims are unverified against the missing "
                "content, not independent evidence of quality"
            )

    # Honest-confidence ceiling per report type (cut_point / loop_detected keep
    # their computed value; fallback verdicts are capped so the UI never shows
    # "100% sure" on a guess).
    confidence = min(confidence, _CONFIDENCE_CAP.get(report_type, 1.0))

    path = propagation_path(inp, cond, culprits[0]) if culprits else []
    cost = downstream_cost(inp, culprits)

    # Unscored ANCESTORS that could genuinely hide the origin — excluding
    # structural roots, whose unscored-ness is by design (they hold no content
    # and cannot be the culprit). Listing a structural root here is what made the
    # UI cap a directly-observed failure with "a hidden node could be the origin"
    # while candidacy simultaneously called that same node "excluded by design".
    unknown_ancestors: set[str] = set()
    for c in culprits:
        if c in cond.graph:
            unknown_ancestors.update(
                n
                for n in nx.ancestors(cond.graph, c)
                if score_map.get(n) is None and not _is_structural_root(n)
            )

    # Deterministic contract violations as their OWN evidence stream (provenance:
    # a hard input/output diff, not the LLM judge). Kept separate from judge_notes
    # so a strong, reproducible signal is never diluted into fluent prose.
    contract_breaches: list[dict] = []
    for n in graph_nodes:
        ns = inp.scores.get(n)
        if ns is not None and ns.contract_violations:
            for key, a, b in ns.contract_violations:
                contract_breaches.append(
                    {
                        "run_id": n,
                        "agent": inp.agent_names.get(n, n),
                        "key": key,
                        "from": a,
                        "to": b,
                    }
                )

    # Named deterministic signals (docs/deterministic-signals.md): node-level
    # entries from scoring, stamped with the node identity. Graph-level entries
    # (tier1 deliverable checks) are appended by the worker after serialization.
    deterministic_signals: list[dict] = []
    for n in graph_nodes:
        ns = inp.scores.get(n)
        if ns is not None and ns.deterministic_signals:
            for sig in ns.deterministic_signals:
                deterministic_signals.append(
                    {
                        **sig,
                        "run_id": n,
                        "agent": inp.agent_names.get(n, n),
                        "provenance": "deterministic",
                    }
                )

    # Cross-check: a deterministic contract breach vs the terminal verdict. The
    # terminal judge sees the deliverable's CONTENT, not the carried contract
    # parameters — so an "ok" terminal does NOT clear a breach that reached the
    # deliverable. Say so explicitly: it is a latent defect the ground truth is
    # blind to, exactly the kind of silent failure this product exists to surface.
    if contract_breaches and terminal_ok:
        detail = "; ".join(
            f"{b['agent']} {b['key']}: {b['from']!r}->{b['to']!r}"
            for b in contract_breaches
        )
        # The terminal section is the report's loudest element — an unqualified
        # "ok 1.00" above a proven mid-pipeline breach makes the header lie by
        # omission. Qualify the verdict AT the verdict, not five rows below.
        # (The worker upgrades this caveat to a VERIFIED shipped/corrected wording
        # once it has checked the deliverable payload.)
        if terminal_evidence is not None:
            terminal_evidence["caveat"] = (
                f"ok in CONTENT only — a contract breach ({detail}) was "
                "introduced mid-pipeline; conformance of the shipped artifact "
                "to the carried contract is unverified at this level (see "
                "contract_vs_terminal / contract_propagation)"
            )
        notes.append(
            f"contract_vs_terminal: a deterministic contract breach ({detail}) "
            f"was introduced mid-pipeline, and the terminal judge still passed "
            f"the run (score={tv.score}, {tv.reasoning!r}). The terminal judge "
            "verifies content, not carried contract parameters, so its ok "
            "verdict cannot clear the breach. Whether the rewritten value "
            "propagated into the final artifact is NOT decidable from node "
            "scores alone (see the contract_propagation note if payload evidence "
            "settled it, else verify out of band) — treat the run as recovered "
            "in content but unverified in contract, not as clean"
        )
    elif contract_breaches and terminal_bad:
        # A bad terminal does NOT automatically corroborate the breach: the
        # terminal may be bad for an unrelated reason (missing content) while
        # the breach is a format fault — two INDEPENDENT faults sharing an
        # origin, not two proofs of one fault. Corroboration is claimed only
        # when the terminal reasoning itself cites the breached parameter.
        reasoning_text = (tv.reasoning or "").casefold()
        cited = [
            b
            for b in contract_breaches
            if str(b["key"]).casefold() in reasoning_text
            or str(b["to"]).casefold() in reasoning_text
        ]
        detail = "; ".join(
            f"{b['agent']} {b['key']}: {b['from']!r}->{b['to']!r}"
            for b in contract_breaches
        )
        if cited:
            notes.append(
                f"contract_vs_terminal: the bad terminal verdict cites the "
                f"breached parameter — the contract breach ({detail}) and the "
                "terminal failure describe the same fault (corroborated)"
            )
        else:
            notes.append(
                f"contract_vs_terminal: a deterministic contract breach "
                f"({detail}) exists AND the terminal verdict is bad — but the "
                "terminal reasoning does not cite the breached parameter, so "
                "these are treated as TWO INDEPENDENT faults sharing an origin "
                "(a content failure does not corroborate a format breach); "
                "remediate both"
            )

    # Show every significant quality drop (> min_drop) against a node's best
    # scored predecessor, not only the candidates — the biggest drop in the graph
    # (e.g. a loop member) must never be missing from this signal.
    # Only OBSERVED drops belong here: a candidate whose base is the assumed 1.0
    # source-like baseline has no measured predecessor, and rendering "-0.85 from
    # best-scored predecessor" against that fiction fabricates a number (the
    # assumption is declared in candidacy instead).
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
    all_drops.update(
        {c.run_id: c.drop for c in candidates if c.drop is not None and not c.base_assumed}
    )
    all_drops.update(loop_drops)

    # Candidacy trace: why each node was or wasn't blamed, WITH the numbers the
    # decision rested on (score vs threshold, drop vs reference, exclusion
    # reason) — an audit trail, not a label. A verdict you cannot audit is one
    # the user will not trust.
    culprit_set = set(culprits)
    gap_basis = {g["run_id"]: g.get("basis") for g in verification_gaps}
    loop_set = set(loop_members)
    chain_pos: dict[str, tuple] = {}
    for ch in analysis.degradation_chains:
        for i, r in enumerate(ch.run_ids):
            chain_pos.setdefault(r, (ch, i))
    t = cfg.threshold
    candidacy: dict[str, str] = {}
    for n in graph_nodes:
        s = score_map.get(n)
        ns = inp.scores.get(n)
        cand = next((c for c in candidates if c.run_id == n), None)
        if report_type == "composition_failure" and n in culprit_set:
            # Headline suspect of a fallback verdict: the orchestration/design
            # LAYER is suspected, not this node's own work. Saying "never a
            # culprit" and "suspect" about the same node was a contradiction.
            candidacy[n] = (
                "suspect (fallback) — no node individually broke; the "
                "orchestration/task-design layer enters the graph here. "
                "Not a proven culprit"
            )
        elif s is None:
            reason = (ns.unscored_reason if ns is not None else None) or "unknown"
            if _is_structural_root(n):
                candidacy[n] = (
                    "structural root — intentionally unscored (orchestrator "
                    "entry point with no output payload); excluded by design, "
                    "not a data-quality problem"
                )
            else:
                candidacy[n] = (
                    f"unscored ({reason}) — excluded: a node without a score can "
                    "never be scored-in or -out as culprit. If this is "
                    "unexpected, fix the instrumentation (see notes)"
                )
        elif gap_basis.get(n) == "verdict_scored_incorrect":
            candidacy[n] = (
                f"verification gap — the verifier's own PASS/FAIL was judged "
                f"wrong (score {s:.2f} < threshold {t:.2f})"
            )
        elif gap_basis.get(n) == "passed_bad_terminal":
            candidacy[n] = (
                f"verification gap — scored healthy ({s:.2f} >= {t:.2f}) yet the "
                "terminal verdict is bad: its PASS let bad work through"
            )
        elif report_type == "degraded_recovered" and n in culprit_set:
            det = (
                "; ".join(
                    f"{k}:{a!r}->{b!r}"
                    for k, a, b in inp.scores[n].contract_violations
                )
                if inp.scores.get(n) and inp.scores[n].contract_violations
                else ""
            )
            if det:
                # The contract check compares the node's OWN observed input to its
                # output: the parameter demonstrably ARRIVED intact, so the fault
                # demonstrably originated here — attribution rests on observation.
                provenance = (
                    "attribution: its input was observed intact (the contract "
                    "parameter arrived correctly), so the rewrite demonstrably "
                    "originated here"
                )
            elif cand is not None and cand.base_assumed:
                provenance = (
                    "attribution: no scored predecessor — the clean 1.00 baseline "
                    "is ASSUMED (structural-root handoff carries no content)"
                )
            else:
                provenance = None
            candidacy[n] = (
                f"degraded here — score {s:.2f} < threshold {t:.2f}"
                + (f", contract violation ({det})" if det else "")
                + " — but every successor recovered and the terminal is ok; a "
                "near-miss (fragile node), not the origin of a live failure"
                + (f". {provenance}" if provenance else "")
            )
        elif n in culprit_set:
            # Every origin line must carry numbers that are TRUE for this node —
            # a candidacy trace that misstates a comparison poisons trust in the
            # whole report. Predecessor context comes from the actual graph.
            pred_scores = [
                score_map[p]
                for p in cond.graph.predecessors(n)
                if score_map.get(p) is not None
            ]
            if n == fabrication_origin:
                f_flags = ", ".join(
                    sorted(_CONTENT_FLAGS.intersection(inp.scores[n].flags))
                )
                candidacy[n] = (
                    f"origin (fabrication cascade) — own judge flagged "
                    f"[{f_flags}]; blended score {s:.2f} stayed above threshold "
                    f"{t:.2f}, but the bad terminal verdict corroborates the "
                    "missing content, and downstream nodes claimed success "
                    "over it"
                )
            elif cand is not None and cand.cumulative_path:
                candidacy[n] = (
                    f"origin — erosion starts here: cumulative drop "
                    f"{cand.drop:.2f} from healthy base {cand.base:.2f} across "
                    f"{' -> '.join(cand.cumulative_path)} (cumulative threshold "
                    f"{cfg.cum_drop_threshold:.2f})"
                )
            elif all_drops.get(n) is not None:
                candidacy[n] = (
                    f"origin — score {s:.2f}, dropped {all_drops[n]:.2f} from its "
                    f"best scored predecessor (gap threshold "
                    f"{cfg.gap_threshold:.2f}, node threshold {t:.2f})"
                )
            elif pred_scores:
                base = max(pred_scores)
                candidacy[n] = (
                    f"origin — score {s:.2f} vs best scored predecessor "
                    f"{base:.2f} (drop {max(0.0, base - s):.2f}, threshold "
                    f"{t:.2f})"
                )
            elif s < t:
                candidacy[n] = (
                    f"origin — score {s:.2f} < threshold {t:.2f} at the "
                    "observable boundary (genuinely no scored predecessor)"
                )
            else:
                candidacy[n] = (
                    f"origin — score {s:.2f}; selected by classification, see "
                    "notes for the evidence"
                )
        elif n in loop_set:
            candidacy[n] = (
                f"loop member — score {s:.2f}, same retry loop as the origin; "
                "blame drilled into the worst member"
            )
        elif s < t:
            candidacy[n] = (
                f"score {s:.2f} < threshold {t:.2f} — inherited degradation, "
                "shadowed by the origin upstream"
            )
        elif n in chain_pos:
            ch, i = chain_pos[n]
            names = " -> ".join(ch.run_ids)
            if i == 0:
                candidacy[n] = (
                    f"degradation-path start — last healthy node ({s:.2f} >= "
                    f"{t:.2f}) before the erosion ({names}, cumulative "
                    f"-{ch.cumulative_drop:.2f}); its output may carry the seed "
                    "of the failure and is worth manual review"
                )
            else:
                candidacy[n] = (
                    f"on the degradation path ({names}) — score {s:.2f} still >= "
                    f"threshold {t:.2f}, part of a cumulative "
                    f"-{ch.cumulative_drop:.2f} erosion"
                )
        elif (
            terminal_bad
            and _is_verifier(inp.agent_names.get(n))
            and ns is not None
            and "issued_fail" in ns.flags
        ):
            candidacy[n] = (
                f"honest whistle-blower — issued FAIL on the bad work (score "
                f"{s:.2f} = verdict correctness); not a gap"
            )
        elif n in reality_conflicts:
            candidacy[n] = (
                f"claims-vs-reality conflict — scored {s:.2f} ('healthy') for "
                "the very artifact the terminal judge rejected; the healthy "
                "score is treated as an unverified claim, not as fact"
            )
        elif n in cascade_participants:
            candidacy[n] = (
                f"fabrication-cascade participant — scored {s:.2f}, but built "
                "on input flagged for missing required content and claimed "
                "success over it; the score is an unverified claim, not "
                "independent evidence"
            )
        elif all_drops.get(n, 0.0) > 0.2:
            candidacy[n] = (
                f"dropped {all_drops[n]:.2f} to {s:.2f} but downstream recovered "
                "— transient low, not a spreading origin"
            )
        else:
            candidacy[n] = f"healthy — score {s:.2f} >= threshold {t:.2f}"

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
        terminal_verdict=terminal_evidence,
        degradation_paths=[
            {
                "path": list(ch.run_ids),
                "scores": list(ch.scores),
                "cumulative_drop": ch.cumulative_drop,
            }
            for ch in analysis.degradation_chains
        ],
        topo_order=list(graph_nodes),
        verifier_run_ids=[
            n for n in graph_nodes if _is_verifier(inp.agent_names.get(n))
        ],
        node_flags={
            n: list(inp.scores[n].flags)
            for n in graph_nodes
            if inp.scores.get(n) is not None and inp.scores[n].flags
        },
        score_overrides=score_overrides,
        observation_confidence=observation_confidence,
        attribution_confidence=attribution_confidence,
        attribution_breakdown=attribution_breakdown,
        contract_violations=contract_breaches,
        deterministic_signals=deterministic_signals,
        topology=topology,
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
