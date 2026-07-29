"""find_blame() orchestration and the first-match-wins classification table
(spec 3.7)."""

from collections.abc import Sequence
from dataclasses import replace

import networkx as nx

from .condense import _chron_key, condense
from .assemble import (
    add_extra_findings,
    add_verifier_findings,
    build_findings,
    ensure_content_drop_finding,
    emit_composition,
    emit_cut_point,
    emit_degraded_recovered,
    emit_external,
    emit_form,
    emit_loop,
    emit_multi,
    emit_terminal_unlocalized,
    emit_verification,
    finalize_schema2,
)
from .derive import derive_escalation_records, derive_report_type
from .confidence import (
    BOUNDARY_ATTRIBUTION_CAP as _BOUNDARY_ATTRIBUTION_CAP,
    CORROBORATED_FLAG,
    REPORT_TYPE_CAP as _CONFIDENCE_CAP,
    _DETERMINISTIC_OBSERVATION,
    compute_confidence,
    compute_observation_confidence,
    report_type_cap,
)
from .cost import downstream_cost
from .cutpoint import Candidate, _analyze
from .finding import Finding
from .loops import _detect_anomalies
from .narrative import (
    CandidacyRecord,
    NoteRecord,
    render_attribution_basis,
    render_candidacy,
    render_notes,
    render_score_override_reason,
    render_terminal_caveat,
    serialize_candidacy,
    serialize_note,
)
from .path import propagation_path
from .roles import is_verifier as _is_verifier
from .topology import classify_topology
from .types import BlameInput, BlameReport, Evidence

# Structured scoring flags that assert a CONTENT defect (the judge admitted the
# node under-delivered). With a bad terminal corroborating them, the earliest
# flagged node marks where required content demonstrably went missing — the
# fabrication-cascade origin (everything downstream claimed success over it).
_CONTENT_FLAGS = frozenset(
    {"missing_required_content", "ignored_instruction", "factual_error"}
)


def _drill_into_loop(cond, inp, super_id, exit_node, preferred=None):
    """When the culprit super-node is a loop (multi-member SCC), the blame belongs
    to the MEMBER where quality actually broke — not the exit node that merely
    flows downstream. Returns (culprit_run_id, members, real_drop) where real_drop
    is the member's drop from its own raw (in-graph) predecessors, so it reflects
    the true break (e.g. act 0.93 -> render 0.27) rather than the loop's exit drop.

    ``preferred`` is the member the intra-SCC localizer qualified
    (``Candidate.scc_member``). It wins over "worst-scoring", because the worst
    member may be the VICTIM of another member rather than the node that broke —
    and because a member picked purely by score can end up with no supporting
    evidence at all (its score above threshold, no measurable drop), which the
    §2.4 defect validator rightly refuses to build a defect from."""
    members = list(cond.super_nodes[super_id].members)
    scored = [
        (m, inp.scores[m].score)
        for m in members
        if inp.scores.get(m) is not None and inp.scores[m].score is not None
    ]
    if len(members) <= 1 or not scored:
        return exit_node, members, None
    if preferred is not None and any(m == preferred for m, _ in scored):
        culprit = preferred
    else:
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


def _violations(inp: BlameInput, run_id: str) -> list[dict]:
    """A node's contract violations as the typed payload the narrative renders
    from — never a pre-formatted detail string handed to a template."""
    ns = inp.scores.get(run_id)
    if ns is None or not ns.contract_violations:
        return []
    return [{"key": k, "from": a, "to": b} for k, a, b in ns.contract_violations]


def _has_deterministic_defect(inp: BlameInput, run_id: str) -> bool:
    """A hard, reproducible signal that this node's output is defective — a
    contract violation or an admitted content flag — as opposed to a graded judge
    opinion. Drives observation_confidence to near-certain."""
    ns = inp.scores.get(run_id)
    if ns is None:
        return False
    return bool(ns.contract_violations) or bool(_CONTENT_FLAGS.intersection(ns.flags))


# The attribution ceiling for a CONTENT defect at the OBSERVABILITY BOUNDARY
# (baseline assumed, not measured) is BOUNDARY_ATTRIBUTION_CAP, read from the
# confidence rules table (imported above as _BOUNDARY_ATTRIBUTION_CAP). "The
# fault originated here" cannot be near-certain about a node whose predecessor
# was never scored. The cap is specific to the content defect: a contract
# violation is exempt entirely, because its input/output diff OBSERVED the
# carried parameter arriving intact and leaving rewritten.


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
        # 0.95 is the OBSERVED-origination convention: the check saw the value
        # arrive intact and leave rewritten. When the candidate was reached only
        # because the upstream node that first put this value in circulation is
        # invisible to this channel, origination was NOT observed and the same
        # cap the headline uses has to apply here — otherwise the verdict prints
        # a confidence no defect of its own supports.
        breakdown.append(
            {
                "defect": "contract_violation",
                "attribution": (
                    inp.config.unknown_confidence_cap
                    if candidate.unknown_upstream
                    else _DETERMINISTIC_OBSERVATION
                ),
                "basis": render_attribution_basis(
                    "contract_violation", {"violations": _violations(inp, run_id)}
                ),
            }
        )
    # A content_degradation defect exists ONLY when the CONTENT channel made this
    # node an origin. A deterministic-only origin (judge-healthy node caught by a
    # hard check) has no content defect — emitting a content_degradation row for
    # it would be a fabricated number under a topological cap, not a measurement.
    if candidate.via in ("content", "both"):
        content_attr = (
            min(raw_attribution, _BOUNDARY_ATTRIBUTION_CAP)
            if candidate.base_assumed
            else raw_attribution
        )
        breakdown.append(
            {
                "defect": "content_degradation",
                "attribution": content_attr,
                "basis": render_attribution_basis(
                    "content_degradation",
                    {
                        "base_assumed": candidate.base_assumed,
                        "cap": _BOUNDARY_ATTRIBUTION_CAP,
                    },
                ),
            }
        )
    return breakdown


def _verdict_attribution(
    inp: BlameInput, run_id: str, candidate, attribution: float,
    notes: list[NoteRecord],
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
        if candidate.unknown_upstream:
            # The 0.95 above is earned by OBSERVED origination — the diff saw the
            # parameter arrive intact and leave rewritten. This candidate is the
            # other case: the value was already in circulation upstream and it is
            # named only because the node that put it there is not reachable in
            # this channel (a non-exit cycle member, or a node excluded as a
            # propagation point). The breach is certain; that it ORIGINATED here
            # is not, so attribution cannot carry the deterministic headline.
            notes.append(
                NoteRecord("attribution_capped", {"cap": inp.config.unknown_confidence_cap})
            )
            return inp.config.unknown_confidence_cap
        return _DETERMINISTIC_OBSERVATION
    if candidate.base_assumed and attribution > _BOUNDARY_ATTRIBUTION_CAP:
        notes.append(
            NoteRecord("attribution_capped", {"cap": _BOUNDARY_ATTRIBUTION_CAP})
        )
        return _BOUNDARY_ATTRIBUTION_CAP
    return attribution


def find_blame(
    inp: BlameInput, *, extra_findings: "Sequence[Finding] | None" = None
) -> BlameReport:
    """Localize blame and project the typed verdict.

    ``extra_findings`` (verdict refactor F2 seam, §F2.2): typed Findings the
    CALLER computed before derivation — fact/contract-propagation and
    required-fact checks the engine cannot compute (it holds no payloads). They
    join the schema-2 findings and take part in the mandatory reconcile pass
    (§2.4), so a fact that disagrees with a terminal/contract finding surfaces a
    divergence at derivation time instead of being printed twice unreconciled.
    Backward compatible: ``None`` reproduces the pre-F2 output exactly.
    """
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
    # Run-level evidence fact: the judged quality channel produced nothing at all
    # (a `--no-judge` pass, or every composite below the minimum weight). Stamped
    # onto every emitted Defect so the projection can tell "the content channel
    # cleared this node" from "the content channel never ran" — without it a
    # deterministic-only verdict derives as degraded_recovered, i.e. an absent
    # measurement read as a passing one.
    quality_unmeasured = all(s is None for s in score_map.values())

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
        | (
            # Rubric split: record the FORM dimension whenever the judge
            # produced one, so the report can show "content ok, form bad"
            # instead of one conflated verdict.
            {"form": {"bad": tv.form_bad, "requirement": tv.form_requirement,
                      "observed": tv.form_observed,
                      "reasoning": tv.form_reasoning}}
            if (tv.form_bad or tv.form_requirement is not None)
            else {}
        )
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

    def _observed_empty(n: str) -> bool:
        """Known to have produced nothing — an OBSERVED absence.

        Distinct from an absent observation, and only the second can hide a
        culprit. A node whose output was recorded empty while its usage reports
        emitted tokens was measured, and the measurement says "no work came out
        of here": there is no room behind it for a defect nobody saw. Counting
        it as an unknown suppressed the classification on the strength of a node
        we know more about than most.
        """
        ns = inp.scores.get(n)
        return ns is not None and ns.unscored_reason == "empty_output"

    hidden_unscored = [
        n for n in unscored if not _is_structural_root(n) and not _observed_empty(n)
    ]

    # The classification rationale as TYPED records (§2.4: no free-prose channel
    # out of decision code). Rendered to sentences once, at the end, by the
    # narrative templates — nothing here writes English.
    notes: list[NoteRecord] = []
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

    # Deterministic contract violations as their OWN evidence stream (provenance:
    # a hard input/output diff, not the LLM judge). Kept separate from judge_notes
    # so a strong, reproducible signal is never diluted into fluent prose.
    # Computed BEFORE the cascade so the schema-2 findings (and the Defect[] the
    # cascade emits) can reference them.
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

    # Schema-2 findings (verifier findings are added later — they depend on the
    # verification gaps, which depend on the culprits the cascade selects). The
    # cascade EMITS the typed Defect[] from what it localizes, referencing these
    # findings; report_type is DERIVED from those defects afterwards.
    # Terminal-review verifier lane: verifiers whose every successor is another
    # verifier (or nothing) review the FINAL work product, so their judge's
    # input_flawed claim measures the same fact the terminal content verdict
    # measures — the §2.4 reconcile can then surface an assessment_conflict
    # (e.g. the byte-stable "missing evidence notes" confabulation: verifier
    # lane says flawed, checkable terminal says ok). A verifier feeding a
    # producer reviews an intermediate artifact — different fact, no key.
    _succ: dict[str, list[str]] = {}
    for _ea, _eb in inp.edges:
        _succ.setdefault(_ea, []).append(_eb)
    assessment_lane = {
        n
        for n in graph_nodes
        if _is_verifier(inp.agent_names.get(n))
        and all(_is_verifier(inp.agent_names.get(s)) for s in _succ.get(n, []))
    }
    # Runs inside a multi-member SCC: their candidate drop is measured at the
    # loop EXIT, but blame drills to the worst member — build_findings skips
    # their build-time drop and the cascade emits the drilled member's real
    # drop (ensure_content_drop_finding) once drilling has decided.
    loop_runs = {
        m
        for sn in cond.super_nodes.values()
        if len(sn.members) > 1
        for m in sn.members
    }
    idx = build_findings(
        inp,
        graph_nodes,
        contract_breaches,
        deterministic_signals,
        [],
        tv,
        anomalies,
        candidates=candidates,
        threshold=cfg.threshold,
        assessment_lane=assessment_lane,
        loop_runs=loop_runs,
    )
    # Caller-supplied findings join the index BEFORE defect emission (§F2.2):
    # a worker-verified breach_propagated is what lets the contract defect cite
    # the evidence for its own "shipped" claim instead of asserting it.
    if extra_findings:
        add_extra_findings(idx, extra_findings)
    via_by_run = {c.run_id: c.via for c in candidates}
    base_assumed_by_run = {c.run_id: c.base_assumed for c in candidates}
    defects: list = []

    def _resolve_culprit(candidate) -> str:
        """The culprit run_id for one candidate, drilling into a cycle when the
        origin sits inside one, and emitting the localization fact its content
        defect will cite.

        Shared by every multi-origin row. Before this existed the multi-culprit
        row explicitly never drilled, so the SAME fault localized differently
        depending on how many faults the graph happened to contain: alone it
        named the loop member that broke, alongside a second origin it named the
        loop's exit — a node that can sit comfortably above the threshold.
        """
        drilled, members, real_drop = _drill_into_loop(
            cond, inp, candidate.super_id, candidate.run_id, candidate.scc_member
        )
        # Drill only in the CONTENT channel: a deterministic origin's evidence is
        # the exit node's own input/output diff and must not move off it. A drill
        # without either a measured drop or a qualified member would leave the
        # content defect with no supporting finding (§2.4 validator).
        if (
            len(members) > 1
            and candidate.via == "content"
            and (real_drop is not None or candidate.scc_member is not None)
        ):
            loop_members.extend(m for m in members if m not in loop_members)
            if real_drop is not None:
                loop_drops[drilled] = real_drop
                _s = _node_score(inp, drilled)
                ensure_content_drop_finding(
                    idx,
                    inp,
                    drilled,
                    score=_s,
                    base=(_s + real_drop) if _s is not None else None,
                    drop=real_drop,
                    loop_members=members,
                )
            return drilled
        # Not drilled: the exit IS the culprit, so restore its own drop as the
        # localization fact (build-time emission is skipped for cycle members).
        if candidate.run_id in loop_runs and (
            candidate.via in ("content", "both") or candidate.cumulative_path
        ):
            ensure_content_drop_finding(
                idx,
                inp,
                candidate.run_id,
                score=candidate.score,
                base=candidate.base,
                drop=candidate.drop,
            )
        return candidate.run_id

    def _conf_candidate(candidate, culprit):
        """Confidence inputs for a drilled culprit: the member's own score and
        in-cycle drop, never the exit's."""
        if culprit == candidate.run_id:
            return candidate
        _s = _node_score(inp, culprit)
        _d = loop_drops.get(culprit, candidate.drop)
        return replace(
            candidate,
            run_id=culprit,
            score=_s if _s is not None else candidate.score,
            drop=_d,
            base=(_s + _d) if (_s is not None and _d is not None) else candidate.base,
        )

    def _stamp(ds: list) -> list:
        """Stamp the run-level ``quality_unmeasured`` fact onto emitted defects.

        Applied at the derivation points rather than threaded through nine
        emitter signatures: it is one fact about the RUN, not a per-emitter
        decision, and every defect of the run carries the same value."""
        if not quality_unmeasured:
            return ds
        return [replace(d, quality_unmeasured=True) for d in ds]

    if quality_unmeasured and not candidates and not anomalies:
        # Row 1: NOTHING was measured — no judged score anywhere, AND no
        # deterministic channel localised anything either.
        #
        # The score half of this condition used to stand alone, ahead of the
        # candidate list, and that cost the product its sharpest silent failure:
        # a trace whose contract check DID observe `format` markdown->html at one
        # node (select_candidates returns that node, the breach is already in
        # evidence.contract_violations) was reported as `unclassified`, zero
        # culprits, confidence 0.0 — the CLI printing "NOT VERIFIED · nothing
        # could be measured" over evidence the engine had in hand. The row
        # predates channel decoupling: "no quality score" is not "no evidence",
        # which is precisely what the deterministic channel exists to disprove.
        # A loop-limit breach (`anomalies`) is deterministic for the same reason
        # and is likewise not an absence of measurement.
        notes.append(NoteRecord("no_scores"))
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
        culprits = [run_id]
        confidence = compute_confidence(candidate, cfg)
        defects = emit_external(
            idx,
            run_id,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        notes.append(NoteRecord("root_cause_external", {"run_id": candidate.run_id}))
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
        # The attempts that actually repeated, when the trace said which they
        # were. Everything caught in the cycle is not the runaway: a nested loop
        # condenses its controllers, its siblings and the nodes downstream of
        # the back-edge into one SCC, and naming all of them names the graph.
        culprits = list(anomaly.repeating_run_ids or anomaly.member_run_ids)
        loop_sid = cond.node_to_super[anomaly.member_run_ids[0]]
        loop_candidate = candidate_sids.get(loop_sid)
        # No candidate: the deterministic limit breach itself is the evidence.
        confidence = compute_confidence(loop_candidate, cfg) if loop_candidate else 1.0
        defects = emit_loop(
            idx,
            culprits[0],
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        notes.append(
            NoteRecord(
                "loop_detected",
                {
                    "iterations": anomaly.iterations,
                    "limit_kind": anomaly.limit_kind,
                    "agents": sorted(
                        {inp.agent_names.get(r, r) for r in anomaly.repeating_run_ids}
                        or set(anomaly.agent_names)
                    ),
                },
            )
        )
        # An anomalous loop does NOT absorb the rest of the graph. This row used
        # to replace every defect with the loop's, so a graph with a retry storm
        # in one branch and an independent break in another reported only the
        # loop — the second fault vanished from culprits, defects and candidacy
        # alike. Retry-heavy topologies (reflection, self-critique) hit that
        # constantly, and from the first anomaly on the rest of the graph was a
        # blind spot.
        other_candidates = [c for c in candidates if c.super_id != loop_sid]
        if other_candidates:
            other_culprits = [_resolve_culprit(c) for c in other_candidates]
            defects = defects + emit_multi(
                idx,
                other_culprits,
                via_by_run=via_by_run,
                base_assumed_by_run=base_assumed_by_run,
                terminal_bad=terminal_bad,
                observation_confidence=observation_confidence,
                attribution_confidence=attribution_confidence,
            )
            culprits = culprits + [c for c in other_culprits if c not in culprits]
            confidence = (
                confidence
                + sum(
                    compute_confidence(
                        _conf_candidate(c, cul), cfg, multi_culprit=True
                    )
                    for c, cul in zip(other_candidates, other_culprits)
                )
            ) / (1 + len(other_candidates))
            notes.append(
                NoteRecord(
                    "independent_origins",
                    {
                        "count": len(other_culprits),
                        "agents": [
                            inp.agent_names.get(c, c) for c in other_culprits
                        ],
                    },
                )
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
        # A recovered origin localized INSIDE a cycle belongs here too: a retry
        # loop whose earlier iteration was bad and whose later one came out
        # healthy is the loop doing its job. Without this the intra-cycle
        # localizer would turn every successful retry into a fresh cut_point —
        # trading one blind spot for a wave of false alarms.
        if candidate.recovered and terminal_ok and (
            len(members_all) <= 1 or candidate.scc_member is not None
        ):
            culprit = candidate.scc_member or candidate.run_id
            # Keyed on the LOCALIZER, not on whether the member happens to be the
            # cycle's exit node. The drop finding is skipped at build time for
            # every cycle member, so an intra-cycle origin that IS the exit would
            # otherwise reach the emitters with no measured drop — and if its own
            # score sits at or above the threshold (a 0.50 that fell from a
            # healthy 1.00 predecessor inside the cycle), the defect ends up with
            # no supporting evidence at all and the §2.4 validator rightly
            # refuses to build it, taking the whole analysis down with a
            # ValueError.
            if candidate.scc_member is not None:
                _dr_score = _node_score(inp, culprit)
                _dr_preds = [
                    score_map[p]
                    for p in cond.graph.predecessors(culprit)
                    if score_map.get(p) is not None
                ]
                _dr_base = max(_dr_preds) if _dr_preds else None
                _dr_drop = (
                    max(0.0, _dr_base - _dr_score)
                    if _dr_base is not None and _dr_score is not None
                    else None
                )
                candidate = replace(
                    candidate,
                    run_id=culprit,
                    score=_dr_score if _dr_score is not None else candidate.score,
                    base=_dr_base,
                    drop=_dr_drop,
                )
                loop_members = members_all
                ensure_content_drop_finding(
                    idx, inp, culprit,
                    score=_dr_score, base=_dr_base, drop=_dr_drop,
                    loop_members=members_all,
                )
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
            defects = emit_degraded_recovered(
                idx,
                culprit,
                via=via_by_run.get(culprit, "content"),
                base_assumed=base_assumed_by_run.get(culprit, False),
                attribution_breakdown=attribution_breakdown,
                observation_confidence=observation_confidence,
                attribution_confidence=attribution_confidence,
            )
            # The localisation clause depends on the CHANNEL (a deterministic
            # origin is localised by the hard check, NOT by a sub-threshold score)
            # — the template picks the wording from `via`, so decision code cannot
            # print "scored 0.89 (below threshold 0.50)" over a healthy judged one.
            notes.append(
                NoteRecord(
                    "degraded_recovered",
                    {
                        "agent": inp.agent_names.get(culprit, culprit),
                        "score": candidate.score,
                        "threshold": cfg.threshold,
                        "via": candidate.via,
                        "violations": _violations(inp, culprit),
                        "terminal_reasoning": tv.reasoning,
                    },
                )
            )
        elif (
            candidate.recovered
            and terminal_bad
            and candidate.via == "deterministic"
            and len(members_all) <= 1
        ):
            # TERMINAL DEFECT, ORIGIN NOT LOCALIZED (terminal rubric split): the
            # terminal verdict reports a CONTENT defect, but the sole candidate
            # is a deterministic-channel origin whose own content the judge
            # scored healthy and whose successors recovered — the content defect
            # observed at the terminal has NO origin in the score map. Calling
            # this a cut_point would pin the content failure on a node the
            # evidence explicitly clears (a verdict claiming a defect its own
            # evidence does not show). The contract fault IS localized here; the
            # content defect is reported as observed-but-unlocalized.
            culprit = candidate.run_id
            culprits = [culprit]
            _raw_attr = compute_confidence(candidate, cfg)
            attribution_confidence = _verdict_attribution(
                inp, culprit, candidate, _raw_attr, notes
            )
            # via=deterministic → breakdown carries the contract row ONLY; no
            # content_degradation row exists to misread as terminal blame.
            attribution_breakdown = _attribution_breakdown(
                inp, culprit, candidate, _raw_attr
            )
            observation_confidence = compute_observation_confidence(
                candidate, cfg, deterministic=_has_deterministic_defect(inp, culprit)
            )
            # Headline is the UNLOCALIZED terminal observation, not the contract
            # attribution — showing 0.95 here would sell the contract fault's
            # certainty as certainty about the content defect's origin.
            confidence = _CONFIDENCE_CAP["terminal_defect_unlocalized"]
            defects = emit_terminal_unlocalized(
                idx, culprit, observation_confidence=observation_confidence
            )
            notes.append(
                NoteRecord(
                    "terminal_defect_unlocalized",
                    {
                        "terminal_reasoning": tv.reasoning,
                        "agent": inp.agent_names.get(culprit, culprit),
                        "violations": _violations(inp, culprit),
                        "score": candidate.score,
                    },
                )
            )
        else:
            culprit, members, real_drop = _drill_into_loop(
                cond, inp, candidate.super_id, candidate.run_id, candidate.scc_member
            )
            culprits = [culprit]
            conf_candidate = candidate
            if len(members) > 1 and real_drop is not None:
                loop_members = members
                # Blame drilled into a loop member; score/drop confidence off the
                # member's real break, not the loop-exit's.
                _member_score = _node_score(inp, culprit)
                if _member_score is None:
                    _member_score = candidate.score
                member_cand = replace(
                    candidate,
                    run_id=culprit,
                    score=_member_score,
                    drop=real_drop,
                    # The member's drop was measured against a REAL in-cycle
                    # predecessor, so the baseline is observed: carry it, or the
                    # predecessor term silently scores 0 and understates a fully
                    # evidenced break.
                    base=_member_score + real_drop,
                    base_assumed=False,
                )
                conf_candidate = member_cand
                loop_drops[culprit] = real_drop
                # The drilled member's real drop IS the localization fact the
                # content defect cites (the exit's drop was skipped at build).
                _c_score = _node_score(inp, culprit)
                ensure_content_drop_finding(
                    idx, inp, culprit,
                    score=_c_score,
                    base=(_c_score + real_drop) if _c_score is not None else None,
                    drop=real_drop,
                    loop_members=members,
                )
                notes.append(
                    NoteRecord(
                        "cut_point",
                        {
                            # "Cycle", not "retry loop": the engine sees no edge
                            # types, and the common case is an orchestrator↔
                            # sub-agent delegation pair, not a retry of anything.
                            "variant": "loop",
                            "run_id": culprit,
                            "score": _node_score(inp, culprit),
                            "drop": real_drop,
                            "members": len(members),
                            "exit_run_id": candidate.run_id,
                            # Only claim "the exit only carried it downstream"
                            # when the drill actually MOVED the blame.
                            "drilled": culprit != candidate.run_id,
                        },
                    )
                )
            elif (
                len(members) > 1
                and candidate.via == "content"
                and not candidate.cumulative_path
            ):
                # Localized inside the cycle, but the member has no scored
                # predecessor to measure against (its upstream is unscored or
                # outside the observed graph). Say that instead of printing a
                # drop the evidence does not contain.
                loop_members = members
                _c_score = _node_score(inp, culprit)
                conf_candidate = replace(
                    candidate,
                    run_id=culprit,
                    score=_c_score if _c_score is not None else candidate.score,
                )
                notes.append(
                    NoteRecord(
                        "cut_point",
                        {
                            "variant": "loop_unmeasured",
                            "run_id": culprit,
                            "score": conf_candidate.score,
                            "members": len(members),
                        },
                    )
                )
            elif candidate.cumulative_path:
                notes.append(
                    NoteRecord(
                        "cut_point",
                        {
                            "variant": "cumulative",
                            "gap_threshold": cfg.gap_threshold,
                            "drop": candidate.drop,
                            "chain": [
                                {
                                    "agent": inp.agent_names.get(r, r),
                                    "score": _node_score(inp, r),
                                }
                                for r in candidate.cumulative_path
                            ],
                            "cum_threshold": cfg.cum_drop_threshold,
                            "run_id": candidate.run_id,
                            "score": candidate.score,
                            "base": candidate.base,
                        },
                    )
                )
            elif candidate.via == "deterministic":
                notes.append(
                    NoteRecord(
                        "cut_point",
                        {
                            "variant": "deterministic",
                            "run_id": candidate.run_id,
                            "score": candidate.score,
                        },
                    )
                )
            elif candidate.base_assumed:
                notes.append(
                    NoteRecord(
                        "cut_point",
                        {
                            "variant": "base_assumed",
                            "run_id": candidate.run_id,
                            "score": candidate.score,
                        },
                    )
                )
            else:
                notes.append(
                    NoteRecord(
                        "cut_point",
                        {
                            "variant": "plain",
                            "run_id": candidate.run_id,
                            "score": candidate.score,
                            "drop": candidate.drop,
                        },
                    )
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
            # Non-drilled SCC candidate (no scored member to drill into): the
            # exit IS the culprit — restore its own drop as the localization
            # fact (build-time emission was skipped for loop members).
            if culprit == candidate.run_id and candidate.run_id in loop_runs and (
                candidate.via in ("content", "both") or candidate.cumulative_path
            ):
                ensure_content_drop_finding(
                    idx, inp, culprit,
                    score=candidate.score, base=candidate.base, drop=candidate.drop,
                )
            defects = emit_cut_point(
                idx,
                culprit,
                via=via_by_run.get(culprit, "content"),
                base_assumed=base_assumed_by_run.get(culprit, False),
                terminal_bad=terminal_bad,
                attribution_breakdown=attribution_breakdown,
                observation_confidence=observation_confidence,
                attribution_confidence=attribution_confidence,
            )
    elif len(candidates) > 1:
        # Row 5: multiple independent candidates. Each is resolved the same way
        # a lone candidate is — including the drill into a cycle — so one fault
        # cannot localize differently just because a second fault exists.
        culprits = [_resolve_culprit(c) for c in candidates]
        confidence = sum(
            compute_confidence(_conf_candidate(c, cul), cfg, multi_culprit=True)
            for c, cul in zip(candidates, culprits)
        ) / len(candidates)
        defects = emit_multi(
            idx,
            culprits,
            via_by_run=via_by_run,
            base_assumed_by_run=base_assumed_by_run,
            terminal_bad=terminal_bad,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        notes.append(
            NoteRecord(
                "multi_culprit",
                {"count": len(candidates), "culprits": list(culprits)},
            )
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
        fabrication_origin = content_flagged[0]
        culprits = [fabrication_origin]
        # Indirect but corroborated evidence (flag + terminal ground truth):
        # stronger than the composition guess (0.4), weaker than a hard score gap.
        confidence = CORROBORATED_FLAG
        defects = emit_cut_point(
            idx,
            fabrication_origin,
            via=via_by_run.get(fabrication_origin, "content"),
            base_assumed=base_assumed_by_run.get(fabrication_origin, False),
            terminal_bad=terminal_bad,
            attribution_breakdown=attribution_breakdown,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        f_ns = inp.scores[fabrication_origin]
        notes.append(
            NoteRecord(
                "cut_point",
                {
                    "variant": "fabrication",
                    "agent": inp.agent_names.get(
                        fabrication_origin, fabrication_origin
                    ),
                    "flags": sorted(_CONTENT_FLAGS.intersection(f_ns.flags)),
                    "terminal_reasoning": tv.reasoning,
                    "terminal_score": tv.score,
                    "others": [
                        inp.agent_names.get(n, n) for n in content_flagged[1:]
                    ],
                },
            )
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
        # Over the RAW nodes, not the super-nodes. A cycle's super-node score is
        # its EXIT member's, so a sub-threshold member inside a cycle used to slip
        # through this guard — and the verdict then asserted "no node individually
        # failed (all scores above threshold)" directly beside a score map showing
        # that member at 0.10. A report may not contradict its own evidence
        # (§11 row 4); the guard has to read what the report renders.
        and all(s >= cfg.threshold for s in score_map.values() if s is not None)
        and not analysis.had_significant_drop
        and cond.sources
    ):
        # Row 6: composition failure — everything looks healthy individually,
        # yet the terminal verdict is bad; blame the source/orchestrator.
        source = min(cond.sources, key=lambda s: _chron_key(inp, cond.super_nodes[s].exit_node))
        culprits = [cond.super_nodes[source].exit_node]
        # Fallback verdict: no node individually broke, so we cannot localise the
        # fault. This is a *suspect* (likely an orchestration/design issue), not a
        # proven culprit — the cap below keeps the reported confidence honest.
        confidence = _CONFIDENCE_CAP["composition_failure"]
        defects = emit_composition(
            idx,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        notes.append(
            NoteRecord(
                "composition_failure",
                {
                    "terminal_reasoning": tv.reasoning,
                    "terminal_score": tv.score,
                    "source": culprits[0],
                },
            )
        )
    else:
        # Row 7 fallback. A negative conclusion needs a trace as much as a
        # positive one: state exactly WHICH precondition ruled each verdict out,
        # so "correctly rejected" is distinguishable from "never ran".
        reasons: list[dict] = []
        if tv is None:
            reasons.append({"code": "no_terminal_verdict"})
        elif not tv.checkable:
            reasons.append({"code": "terminal_not_checkable"})
        elif not tv.bad:
            reasons.append({"code": "terminal_ok", "score": tv.score})
        if hidden_unscored:
            reasons.append(
                {
                    "code": "hidden_unscored",
                    "agents": sorted(
                        inp.agent_names.get(n, n) for n in hidden_unscored
                    ),
                }
            )
        if analysis.had_significant_drop:
            reasons.append({"code": "significant_drop_shadowed"})
        unhealthy = sorted(
            inp.agent_names.get(n, n)
            for n in graph_nodes
            if (u := score_map.get(n)) is not None and u < cfg.threshold
        )
        if unhealthy:
            reasons.append({"code": "unhealthy_not_origin", "agents": unhealthy})
        if not reasons:
            reasons.append({"code": "no_failure_signal"})
        notes.append(NoteRecord("unclassified", {"reasons": reasons}))

    # Honesty: a terminal verdict whose deliverable the judge could not see is
    # not evidence of anything. Say so plainly instead of letting a fabricated
    # "bad terminal" silently vanish or, worse, drive a culprit. A STALE verdict
    # gets its own narrative — the problem is not instrumentation, it is that
    # the verdict's deterministic basis no longer reproduces.
    if tv is not None and not terminal_checkable:
        _tv_data = {"bad": tv.bad, "score": tv.score, "reasoning": tv.reasoning}
        notes.append(
            NoteRecord(
                "terminal_stale" if tv.stale else "terminal_not_checkable", _tv_data
            )
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
            NoteRecord("instrumentation_warning", {"agents": missing_payload})
        )

    # NOT the same finding, and it used to be filed as one: a node whose output
    # was recorded EMPTY while its usage reports emitted tokens is not a blind
    # spot in the exporter, it is an agent that spent and shipped nothing. It
    # rides the deterministic channel as an `empty_output` signal; this note is
    # what stops it being read as advice to go fix working instrumentation.
    empty_output = sorted(
        inp.agent_names.get(n, n)
        for n in graph_nodes
        if (ens := inp.scores.get(n)) is not None
        and ens.unscored_reason == "empty_output"
    )
    if empty_output:
        notes.append(NoteRecord("empty_output", {"agents": empty_output}))

    # Topology-driven instrumentation-quality warning (same family as
    # payload_missing). The ONLY behavioral use of the advisory classification:
    # a disconnected graph means edges between components were never recorded.
    if topology["primary"] == "disconnected":
        notes.append(
            NoteRecord("topology", {"components": topology["components"]})
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
                    NoteRecord(
                        "verdict_conflict",
                        {
                            "terminal_reasoning": tv.reasoning,
                            "agent": inp.agent_names.get(
                                _sn.exit_node, _sn.exit_node
                            ),
                            "score": _sn.score,
                        },
                    )
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

    # The verification gaps are now known → their findings can join the index
    # (kept last so finding indices are stable) and the report_type can be
    # DERIVED from the Defect[] the cascade emitted. This is the single source of
    # truth (§2.3): report_type is a projection of the typed defects, never a
    # string the cascade decided that could disagree with its own evidence.
    add_verifier_findings(idx, inp, verification_gaps)
    defects = _stamp(defects)
    report_type = derive_report_type(defects)

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
            "reason": render_score_override_reason(),
        }
        for g in verification_gaps
        if g.get("basis") == "passed_bad_terminal"
        and score_map.get(g["run_id"]) is not None
    ]
    # NOTE (channel decoupling): producers get NO claimed→effective override.
    # The judged score is never overwritten by a deterministic fault, so there is
    # no "claimed" number to strike through — the judged score IS the score, and
    # the hard check localises blame through the engine's deterministic channel
    # (candidacy via="deterministic": "judged 0.89 · contract check FAILED"). The
    # override vehicle below survives ONLY for verifiers whose PASS is refuted by
    # terminal ground truth (a distinct, still-valid mechanism).

    # When nothing localised as an origin but verifiers issued a wrong verdict,
    # the rubber-stamping (or false-alarming) verifiers ARE the failure —
    # retroactively blame them. The note must state each gap's ACTUAL basis and
    # only invoke "the terminal output is bad" when the terminal genuinely is bad
    # AND checkable: a verdict_scored_incorrect gap rests on the role-aware judge
    # scoring the verifier's own PASS/FAIL wrong, NOT on the terminal. Asserting a
    # bad terminal here — worse, while quoting an OK verdict's reasoning — is the
    # exact dishonesty this fixes (a false verification_gap on a healthy run).
    if report_type in ("composition_failure", "unclassified") and verification_gaps:
        culprits = [g["run_id"] for g in verification_gaps]
        confidence = _CONFIDENCE_CAP["verification_gap"]
        # The rubber-stamping (or false-alarming) verifiers ARE the failure now:
        # replace the (composition/none) primary defects with the localized
        # verification defects and re-derive — report_type becomes verification_gap
        # BECAUSE the evidence changed, not by a string reassignment.
        defects = emit_verification(
            idx,
            culprits,
            observation_confidence=observation_confidence,
            attribution_confidence=attribution_confidence,
        )
        defects = _stamp(defects)
        report_type = derive_report_type(defects)
        notes.append(
            NoteRecord(
                "verification_gap",
                {
                    "gaps": [
                        {
                            "agent": g["agent_name"],
                            "score": score_map.get(g["run_id"]),
                            "basis": g["basis"],
                            "issued_fail": "issued_fail"
                            in (
                                inp.scores[g["run_id"]].flags
                                if inp.scores.get(g["run_id"]) is not None
                                else ()
                            ),
                        }
                        for g in verification_gaps
                    ],
                    "threshold": cfg.threshold,
                    # Only quote the terminal as ground truth when it genuinely
                    # is one — a gap resting on the role-aware score alone must
                    # not borrow the terminal's authority.
                    "terminal": (
                        "bad" if terminal_bad else "ok" if terminal_ok else None
                    ),
                    "terminal_reasoning": tv.reasoning if tv is not None else None,
                    "terminal_score": tv.score if tv is not None else None,
                },
            )
        )

    # Escalation (§2.3, single home): a degraded_recovered verdict over a breach
    # the WORKER VERIFIED as propagated (breach_propagated finding, deterministic)
    # escalates to shipped_with_latent_defect INSIDE the single pass — the worker
    # never edits a verdict, and the escalated headline is backed by the same
    # finding the contract defect cites as support.
    _propagated_findings = [f for f in idx.findings if f.kind == "breach_propagated"]
    # The verified-shipped breaches, kept so the CANDIDACY of the escalated
    # origin is written in the same pass from the same evidence. The worker used
    # to overwrite the engine's "near-miss" candidacy AFTER the fact — a second
    # writer whose text stood in self-negation next to the escalated headline
    # (§11 row 6). Deriving it here makes that state unreachable.
    _shipped_breaches: list[dict] = []
    if _propagated_findings:
        _shipped_breaches = [
            {
                "key": f.data.get("key"),
                "from": f.data.get("from"),
                "to": f.data.get("to"),
                "basis": f.data.get("basis"),
            }
            for f in _propagated_findings
        ]
        report_type, _esc_notes = derive_escalation_records(
            report_type,
            [
                {
                    "key": f.data.get("key"),
                    "from": f.data.get("from"),
                    "to": f.data.get("to"),
                    "basis": f.data.get("basis"),
                    "status": "propagated",
                }
                for f in _propagated_findings
            ],
        )
        notes.extend(_esc_notes)

    # Terminal rubric split — FORM dimension. A bad form verdict (the shipped
    # deliverable's form does not match the explicitly requested one) is a
    # DESIGN-level gap, never an individual verifier's: no verifier charter in
    # the graph covers form/contract vision (they verify content), so no
    # verification gap is opened on any of them for a form miss. Without this
    # note the form breach would either vanish (content ok) or masquerade as
    # rubber-stamping (content bad).
    if tv is not None and tv.checkable and tv.form_bad:
        # A form defect is a design-level annotation: it adds a latent_defect
        # incident downstream but never changes the PRIMARY report_type, so it is
        # appended without re-deriving.
        defects.extend(_stamp(emit_form(idx, tv)))
        notes.append(
            NoteRecord(
                "form_defect_shipped",
                {
                    "requirement": tv.form_requirement,
                    "observed": tv.form_observed,
                },
            )
        )

    # Requirement provenance (terminal rubric split): reconcile the
    # deterministic contract reference against the requirement the terminal
    # judge read VERBATIM from the initial input. When the contract's "from"
    # value does not appear in that quote, the reference is NOT
    # user-request-derived — it is harness scaffold or an upstream rewrite —
    # and "verified against the contract" must not be read as "verified
    # against the user's ask". Printing both references side by side without
    # this reconcile is the report-#1 error family.
    if tv is not None and tv.form_requirement:
        _req_lower = tv.form_requirement.lower()
        for _pn in graph_nodes:
            _pns = inp.scores.get(_pn)
            if _pns is None or not _pns.contract_violations:
                continue
            for _pk, _pfrom, _pto in _pns.contract_violations:
                if _pfrom is None or str(_pfrom).lower() in _req_lower:
                    continue
                notes.append(
                    NoteRecord(
                        "requirement_provenance",
                        {
                            "key": _pk,
                            "agent": inp.agent_names.get(_pn, _pn),
                            "from": _pfrom,
                            "to": _pto,
                            "requirement": tv.form_requirement,
                        },
                    )
                )

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
                    NoteRecord(
                        "claims_vs_reality",
                        {
                            "agent": inp.agent_names.get(m, m),
                            "score": ms,
                            "terminal_reasoning": tv.reasoning,
                        },
                    )
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
            notes.append(
                NoteRecord(
                    "cascade_participants",
                    {
                        "agents": [
                            inp.agent_names.get(n, n) for n in cascade_participants
                        ]
                    },
                )
            )

    # Honest-confidence ceiling per report type (cut_point / loop_detected keep
    # their computed value; fallback verdicts are capped so the UI never shows
    # "100% sure" on a guess).
    confidence = min(confidence, report_type_cap(report_type))

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

    # Cross-check: a deterministic contract breach vs the terminal verdict. The
    # terminal judge sees the deliverable's CONTENT, not the carried contract
    # parameters — so an "ok" terminal does NOT clear a breach that reached the
    # deliverable. Say so explicitly: it is a latent defect the ground truth is
    # blind to, exactly the kind of silent failure this product exists to surface.
    if contract_breaches and terminal_ok:
        # The terminal section is the report's loudest element — an unqualified
        # "ok 1.00" above a proven mid-pipeline breach makes the header lie by
        # omission. Qualify the verdict AT the verdict, not five rows below.
        # (The worker upgrades this caveat to a VERIFIED shipped/corrected wording
        # once it has checked the deliverable payload.)
        if terminal_evidence is not None:
            terminal_evidence["caveat"] = render_terminal_caveat(contract_breaches)
        notes.append(
            NoteRecord(
                "contract_vs_terminal",
                {
                    "variant": "terminal_ok",
                    "breaches": contract_breaches,
                    "terminal_score": tv.score,
                    "terminal_reasoning": tv.reasoning,
                },
            )
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
        notes.append(
            NoteRecord(
                "contract_vs_terminal",
                {
                    "variant": "corroborated" if cited else "independent",
                    "breaches": contract_breaches,
                    "terminal_score": tv.score,
                    "terminal_reasoning": tv.reasoning,
                },
            )
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
    # Nodes a localised culprit actually reaches — the only ones whose low score
    # may honestly be called "inherited / shadowed by the origin upstream".
    downstream_of_any_culprit: set[str] = set()
    for _c in culprit_set:
        if _c in cond.graph:
            downstream_of_any_culprit |= nx.descendants(cond.graph, _c)
    gap_basis = {g["run_id"]: g.get("basis") for g in verification_gaps}
    loop_set = set(loop_members)
    chain_pos: dict[str, tuple] = {}
    for ch in analysis.degradation_chains:
        for i, r in enumerate(ch.run_ids):
            chain_pos.setdefault(r, (ch, i))
    t = cfg.threshold
    candidacy: dict[str, CandidacyRecord] = {}
    for n in graph_nodes:
        s = score_map.get(n)
        ns = inp.scores.get(n)
        cand = next((c for c in candidates if c.run_id == n), None)
        if report_type == "composition_failure" and n in culprit_set:
            # Headline suspect of a fallback verdict: the orchestration/design
            # LAYER is suspected, not this node's own work. Saying "never a
            # culprit" and "suspect" about the same node was a contradiction.
            candidacy[n] = CandidacyRecord("composition_suspect")
        elif s is None and not (
            # An unscored node CAN be a culprit — but only through the
            # deterministic channel, which localises without the judge. Falling
            # into the generic "unscored: never a candidate" line for such a node
            # made the report contradict its own headline: the verdict named the
            # agent while its candidacy row said it was never in the running.
            n in culprit_set
            and cand is not None
            and cand.via == "deterministic"
        ):
            if _is_structural_root(n):
                candidacy[n] = CandidacyRecord("structural_root")
            else:
                candidacy[n] = CandidacyRecord(
                    "unscored",
                    {
                        "reason": (ns.unscored_reason if ns is not None else None)
                        or "unknown"
                    },
                )
        elif gap_basis.get(n) == "verdict_scored_incorrect":
            candidacy[n] = CandidacyRecord(
                "gap_verdict_scored_incorrect", {"score": s, "threshold": t}
            )
        elif gap_basis.get(n) == "passed_bad_terminal":
            candidacy[n] = CandidacyRecord(
                "gap_passed_bad_terminal", {"score": s, "threshold": t}
            )
        elif report_type == "shipped_with_latent_defect" and n in culprit_set:
            # Escalation rewrites the NARRATIVE, not just the verdict type — and
            # it does so in the SAME pass, from the breach findings that caused
            # the escalation. This branch fires only for a shipped CONTRACT
            # breach, so the origin is deterministic: lead with the hard check,
            # never "degraded here (score X)" (the judged score is untouched and
            # typically above threshold).
            candidacy[n] = CandidacyRecord(
                "origin_escalated", {"score": s, "shipped": _shipped_breaches}
            )
        elif report_type == "degraded_recovered" and n in culprit_set:
            candidacy[n] = CandidacyRecord(
                "degraded_recovered",
                {
                    "score": s,
                    "threshold": t,
                    "via": cand.via if cand is not None else None,
                    "violations": _violations(inp, n),
                    "base_assumed": cand is not None and cand.base_assumed,
                },
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
                candidacy[n] = CandidacyRecord(
                    "origin_fabrication",
                    {
                        "flags": sorted(
                            _CONTENT_FLAGS.intersection(inp.scores[n].flags)
                        ),
                        "score": s,
                        "threshold": t,
                    },
                )
            elif cand is not None and cand.via == "deterministic":
                candidacy[n] = CandidacyRecord(
                    "origin_deterministic",
                    {"violations": _violations(inp, n), "score": s},
                )
            elif cand is not None and cand.cumulative_path:
                candidacy[n] = CandidacyRecord(
                    "origin_cumulative",
                    {
                        "drop": cand.drop,
                        "base": cand.base,
                        "path": list(cand.cumulative_path),
                        "cum_threshold": cfg.cum_drop_threshold,
                    },
                )
            elif all_drops.get(n) is not None:
                candidacy[n] = CandidacyRecord(
                    "origin_drop",
                    {
                        "score": s,
                        "drop": all_drops[n],
                        "gap_threshold": cfg.gap_threshold,
                        "threshold": t,
                        # "Quality was fine going in" is measured over the inputs
                        # we could score. Naming the ones we could not is the
                        # difference between an observed and an assumed handoff.
                        "unmeasured_inputs": [
                            inp.agent_names.get(u, u) for u in cand.unmeasured_inputs
                        ]
                        if cand is not None and cand.unmeasured_inputs
                        else [],
                    },
                )
            elif pred_scores:
                base = max(pred_scores)
                candidacy[n] = CandidacyRecord(
                    "origin_vs_predecessor",
                    {
                        "score": s,
                        "base": base,
                        "drop": max(0.0, base - s),
                        "threshold": t,
                    },
                )
            elif s < t:
                candidacy[n] = CandidacyRecord(
                    "origin_boundary", {"score": s, "threshold": t}
                )
            else:
                candidacy[n] = CandidacyRecord(
                    "origin_by_classification", {"score": s}
                )
        elif n in loop_set:
            candidacy[n] = CandidacyRecord("loop_member", {"score": s})
        elif s < t:
            # "Shadowed by the origin upstream" is a CLAIM about the graph: it
            # may only be made when a culprit really is an ancestor of this node.
            # Printed unconditionally it turned every sub-threshold node into
            # "inherited degradation" even in reports that localized no origin at
            # all — the reader was told to look upstream at nothing.
            if n in downstream_of_any_culprit:
                candidacy[n] = CandidacyRecord(
                    "inherited", {"score": s, "threshold": t}
                )
            elif culprit_set:
                candidacy[n] = CandidacyRecord(
                    "independent_low", {"score": s, "threshold": t}
                )
            else:
                pred_scores = [
                    score_map[p]
                    for p in cond.graph.predecessors(n)
                    if score_map.get(p) is not None
                ]
                base = max(pred_scores) if pred_scores else None
                candidacy[n] = CandidacyRecord(
                    "below_not_origin",
                    {
                        "score": s,
                        "threshold": t,
                        "base": base,
                        "gap_threshold": cfg.gap_threshold,
                        "why": (
                            "no_predecessor"
                            if base is None
                            else "predecessor_also_low"
                            if base < t
                            else "drop_under_gap"
                        ),
                    },
                )
        elif n in chain_pos:
            ch, i = chain_pos[n]
            candidacy[n] = CandidacyRecord(
                "degradation_path_start" if i == 0 else "degradation_path_member",
                {
                    "score": s,
                    "threshold": t,
                    "path": list(ch.run_ids),
                    "cumulative_drop": ch.cumulative_drop,
                },
            )
        elif (
            terminal_bad
            and _is_verifier(inp.agent_names.get(n))
            and ns is not None
            and "issued_fail" in ns.flags
        ):
            candidacy[n] = CandidacyRecord("whistleblower", {"score": s})
        elif n in reality_conflicts:
            candidacy[n] = CandidacyRecord("claims_conflict", {"score": s})
        elif n in cascade_participants:
            candidacy[n] = CandidacyRecord("cascade_participant", {"score": s})
        elif all_drops.get(n, 0.0) > 0.2:
            candidacy[n] = CandidacyRecord(
                "transient_low", {"drop": all_drops[n], "score": s}
            )
        else:
            candidacy[n] = CandidacyRecord("healthy", {"score": s, "threshold": t})

    # --- Schema-2 typed layers: the Defect[] the cascade EMITTED as it localized
    # (report_type above is a projection of these via derive_report_type). Append
    # any caller findings, run the mandatory reconcile pass, and serialize.
    # extra_findings were indexed into ``idx`` before emission (add_extra_findings)
    # so defects could reference them; finalize only reconciles + validates.
    schema2_findings, schema2_defects = finalize_schema2(idx, defects)

    # Prose is rendered ONCE, here, from the typed records — a single pass with
    # no second writer (§2.4). ``notes``/``candidacy`` keep their string shape
    # for the legacy renderer and for grep/export; ``note_records`` and
    # ``candidacy_records`` are the machine-readable originals consumers key off
    # instead of parsing sentences back.
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
        notes=render_notes(notes),
        note_records=[serialize_note(n) for n in notes],
        manifestation_run_ids=manifestation,
        verification_gaps=verification_gaps,
        candidacy={n: render_candidacy(c) for n, c in candidacy.items()},
        candidacy_records={
            n: serialize_candidacy(c) for n, c in candidacy.items()
        },
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
        schema=2,
        findings=schema2_findings,
        defects=schema2_defects,
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
