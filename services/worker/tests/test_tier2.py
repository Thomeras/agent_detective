"""Tier 2: idempotence, the flagship silent-hallucination diamond, and the
sampled-unclassified rule (no incident for healthy sampled graphs)."""

import asyncio

import pytest

from worker.tier2 import Tier2Processor, classify_incident
from worker.types import STREAM_INCIDENTS_CREATED, Tier1Verdict, Tier2Message

from conftest import (
    FakeJudge,
    FakeObjectStore,
    FakeRepo,
    FakeStreams,
    make_bundle,
    make_settings,
    make_run,
    uid,
)

FAKE_PRICE = "$9.99 FAKE"


def _diamond_repo() -> FakeRepo:
    """orchestrator -> {scraper, translator} -> compliance -> publisher.

    The scraper fabricates a price; compliance and publisher faithfully carry
    it downstream (silent hallucination). Every run reports status=ok.
    """
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", input_inline="scrape 3 products", end_time=1.0),
                make_run(
                    2,
                    "scraper-agent",
                    output_inline=f'{{"price": "{FAKE_PRICE}"}}',
                    end_time=2.0,
                ),
                make_run(3, "translator-agent", output_inline="polskie tlumaczenie", end_time=3.0),
                make_run(
                    4,
                    "compliance-agent",
                    output_inline=f"compliance approved price {FAKE_PRICE}",
                    end_time=4.0,
                ),
                make_run(
                    5,
                    "publisher-agent",
                    output_inline=f"published listing at {FAKE_PRICE}",
                    end_time=5.0,
                ),
            ],
            [(1, 2), (1, 3), (2, 4), (3, 4), (4, 5)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.2,
        terminal_judge_reasoning="final price looks fabricated",
        flags=[],
        flagged=True,
        sampled=False,
    )
    return repo


def _diamond_judge() -> FakeJudge:
    return FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "scraper-agent": {"task_score": 0.1, "input_flawed": False, "reasoning": "fabricated"},
            "translator-agent": {"task_score": 0.95, "input_flawed": False, "reasoning": "ok"},
            "compliance-agent": {"task_score": 0.95, "input_flawed": True, "reasoning": "bad input"},
            "publisher-agent": {"task_score": 0.95, "input_flawed": True, "reasoning": "bad input"},
        },
        claims=[FAKE_PRICE],
    )


def _make_processor(repo, judge=None, settings=None):
    streams = FakeStreams()
    processor = Tier2Processor(
        repo, FakeObjectStore(), streams, judge or FakeJudge(), settings or make_settings()
    )
    return processor, streams


def test_classify_incident_prioritizes_blame_over_terminal():
    assert classify_incident("cut_point", [], True) == ("degraded_quality", "degraded_quality")
    assert classify_incident("loop_detected", [], False)[0] == "loop_detected"
    assert classify_incident("unclassified", ["failed_runs"], False)[0] == "terminal_failure"
    assert classify_incident("unclassified", ["cost_overrun"], False)[0] == "cost_overrun"
    assert classify_incident("unclassified", [], True)[0] == "terminal_failure"
    assert classify_incident("unclassified", [], False) == (None, None)


def test_flagship_diamond_produces_cut_point_incident_naming_scraper():
    repo = _diamond_repo()
    processor, streams = _make_processor(repo, judge=_diamond_judge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    assert len(repo.incidents) == 1
    (key, incident), = repo.incidents.items()
    assert key == (uid(1), "degraded_quality")

    report = repo.blame_reports[0]
    assert report["report_type"] == "cut_point"
    assert report["culprit_run_ids"] == [uid(2)]  # the scraper run
    # Path scraper -> compliance -> publisher.
    assert report["propagation_path"] == [uid(2), uid(4), uid(5)]
    assert report["confidence"] > 0

    # The culprit run belongs to the scraper agent.
    culprit_agent = next(r.agent_name for r in repo.bundles[uid(1)].runs if r.run_id == uid(2))
    assert culprit_agent == "scraper-agent"

    # Fact-propagation evidence names the fabricated value in downstream nodes.
    fp = report["evidence"]["fact_propagation"]
    assert fp and fp[0]["claim"] == FAKE_PRICE
    assert set(fp[0]["found_in"]) == {str(uid(4)), str(uid(5))}

    # The scraper node scored well below the threshold and is the low point.
    assert repo.node_scores[uid(2)].quality_score < 0.5
    assert repo.node_scores[uid(4)].quality_score > 0.5

    messages = streams.messages(STREAM_INCIDENTS_CREATED)
    assert len(messages) == 1
    assert messages[0]["is_new"] is True
    assert messages[0]["incident_id"] == incident["id"]


def test_deliverable_integrity_signals_land_in_evidence_deduped():
    """The engine assembles the deliverable producer's NODE-level integrity
    signal into evidence; tier2's graph-level recompute over the same payload
    must dedupe by (name, run_id, basis) instead of duplicating it."""
    meta = (
        '[{"path": "out/report.docx", "size": 5000, "declared_ext": "docx",'
        ' "detected_kind": "text", "parse_ok": true, "nonempty": true}]'
    )
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", end_time=1.0),
                make_run(2, "writer", output_inline="rendered the report",
                         end_time=2.0, artifact_meta=meta),
            ],
            [(1, 2)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.0,
        terminal_judge_reasoning="deterministic artifact integrity failure",
        flags=["artifact_integrity"],
        flagged=True,
        sampled=False,
    )
    processor, _streams = _make_processor(repo)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    # R4: the judged score is NOT floored — the integrity fault is carried as a
    # deterministic signal (evidence stream), not smuggled into the quality scalar.
    assert repo.node_scores[uid(2)].quality_score > 0.5
    assert "artifact_integrity_fail" not in repo.node_scores[uid(2)].score_components

    report = repo.blame_reports[0]
    sigs = report["evidence"]["deterministic_signals"]
    assert len(sigs) == 1  # node-level entry; the graph-level recompute deduped
    sig = sigs[0]
    assert sig["name"] == "artifact_integrity_fail"
    assert sig["run_id"] == str(uid(2))
    assert sig["agent"] == "writer"
    assert sig["provenance"] == "deterministic"
    assert sig["basis"] == "magic bytes: detected_kind=text for out/report.docx"


def test_processing_same_graph_twice_yields_exactly_one_incident():
    repo = _diamond_repo()
    processor, streams = _make_processor(repo, judge=_diamond_judge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))

    asyncio.run(processor.process(msg))
    asyncio.run(processor.process(msg))  # redelivery

    assert len(repo.incidents) == 1
    assert len(repo.blame_reports) == 1
    assert repo.jobs[str(uid(1))]["status"] == "done"
    # is_new alert emitted only once (second call is skipped at the claim).
    assert len(streams.messages(STREAM_INCIDENTS_CREATED)) == 1


def test_second_claim_is_skipped_when_job_already_done():
    repo = _diamond_repo()
    processor, streams = _make_processor(repo, judge=_diamond_judge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))
    incidents_before = dict(repo.incidents)

    # A fresh streams object: nothing should be published on the skip path.
    processor2, streams2 = _make_processor(repo, judge=_diamond_judge())
    asyncio.run(processor2.process(msg))
    assert repo.incidents == incidents_before
    assert streams2.messages(STREAM_INCIDENTS_CREATED) == []


def test_sampled_unclassified_healthy_graph_creates_no_incident():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", end_time=1.0),
                make_run(2, "worker", output_inline="a clean healthy result", end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="ok",
        terminal_judge_score=0.95,
        terminal_judge_reasoning="fine",
        flags=[],
        flagged=False,
        sampled=True,
    )
    processor, streams = _make_processor(repo, judge=FakeJudge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="sampled", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    assert repo.incidents == {}
    assert repo.blame_reports == []
    assert streams.messages(STREAM_INCIDENTS_CREATED) == []
    assert repo.jobs[str(uid(1))]["status"] == "done"
    # Node scores are still persisted even without an incident.
    assert repo.node_scores[uid(2)].quality_score is not None


def _divergence_report(confidence: float = 0.65):
    """A single-origin fabrication-cascade report over think -> act -> qa: 't' is
    the reported origin, 'a' (act) is the last producer, 'q' (qa) a verifier."""
    from blame_engine import BlameReport, Evidence

    evidence = Evidence(
        score_map={"t": 0.67, "a": 0.9, "q": 0.95},
        drops={}, judge_notes={"q": "correctly passed"}, error_span_ids={},
        loop_anomalies=[], unknown_ancestors=[], fact_propagation=None,
        notes=[],
        # The fabrication-cascade row as the TYPED record the reconciler reads;
        # the rendered sentence is irrelevant to it (and must stay that way).
        note_records=[{"slug": "cut_point", "data": {"variant": "fabrication"}}],
        topo_order=["t", "a", "q"],
    )
    report = BlameReport(
        report_type="cut_point", culprit_run_ids=["t"], propagation_path=["t", "a"],
        confidence=confidence, evidence=evidence, downstream_cost_usd=1.0,
        unscored_run_ids=[],
    )
    names = {"t": "think", "a": "act", "q": "qa"}
    return report, names


def test_reconcile_evidence_flags_tension_and_divergence():
    """Contradicting evidence streams are stated; an empty terminal vs a reviewed
    artifact yields a divergence hint."""
    from worker.tier2 import reconcile_evidence

    report, names = _divergence_report()
    fact_prop = [{"claim": "x", "found_in": ["a"], "not_checkable": []}]

    confidence, notes, _hyp = reconcile_evidence(
        report, fact_prop, names, ["degenerate_output"]
    )
    assert {"evidence_tension", "representation_divergence"} <= {n.slug for n in notes}

    # No tension when claims only matched outside the last producer; no
    # divergence without the degenerate-output flag: nothing contests the origin,
    # so confidence stands and no hypotheses breakdown is emitted.
    confidence2, notes2, hyp2 = reconcile_evidence(
        report, [{"claim": "x", "found_in": [], "not_checkable": []}], names, []
    )
    assert confidence2 == 0.65 and notes2 == [] and hyp2 == []


def test_reconcile_evidence_splits_confidence_across_competing_origins():
    """The docx defect: a report cannot state a single origin at full 0.65 while
    its own divergence note says the true origin may be later. The guard must
    lower the headline confidence to the dominant hypothesis's share AND expose
    the competing origins with weights that sum to 1.0."""
    from worker.tier2 import reconcile_evidence

    report, names = _divergence_report(confidence=0.65)
    fact_prop = [{"claim": "x", "found_in": ["a"], "not_checkable": []}]

    confidence, notes, hypotheses = reconcile_evidence(
        report, fact_prop, names, ["degenerate_output"]
    )

    # Headline confidence dropped to the dominant (reported) origin's share, not
    # a flat 0.65 over two live hypotheses.
    assert confidence == round(0.65 * 0.6, 4)  # 0.39
    assert confidence < 0.65
    split = next(n for n in notes if n.slug == "competing_origins")
    assert split.data["origin_agent"] == "think"
    assert split.data["alt_agent"] == "act"

    # Explicit breakdown: reported origin 'think', later origin 'act' (the last
    # producer), and an unresolved remainder — weights sum to exactly 1.0.
    assert [h["origin"] for h in hypotheses] == ["t", "a", None]
    assert hypotheses[0]["weight"] == 0.39   # dominant reported origin
    assert hypotheses[1]["weight"] == 0.26   # later render/export candidate
    assert hypotheses[2]["origin"] is None   # unresolved remainder
    assert hypotheses[2]["weight"] == round(1.0 - 0.39 - 0.26, 4)
    assert sum(h["weight"] for h in hypotheses) == pytest.approx(1.0)
    # The reported origin stays dominant but never owns the whole confidence.
    assert hypotheses[0]["weight"] > hypotheses[1]["weight"]


def test_reconcile_evidence_divergence_alone_splits_without_tension():
    """representation_divergence on its own (no fact-propagation tension) is
    enough to unsettle the origin: the empty terminal beside a reviewed artifact
    means the loss may be at render/export, so the single origin must not stand
    at full confidence."""
    from worker.tier2 import reconcile_evidence

    report, names = _divergence_report(confidence=0.65)

    confidence, notes, hypotheses = reconcile_evidence(
        report, None, names, ["degenerate_output"]
    )

    slugs = {n.slug for n in notes}
    assert "evidence_tension" not in slugs
    assert "representation_divergence" in slugs
    assert confidence == round(0.65 * 0.6, 4)
    assert [h["origin"] for h in hypotheses] == ["t", "a", None]
    assert "representation_divergence" in hypotheses[1]["basis"]


# --- contract_propagation_check: deterministic verification that a contract ---
# --- breach did (or did not) propagate into the final deliverable          ---
#
# Asserted on the STRUCTURED result (status + basis + the record's typed payload),
# never on the rendered sentence. The status is what drives the escalation, so it
# is the thing a regression must be caught on; the prose is a render artifact and
# is covered once, in test_narrative.py.

_VIOLATION = {
    "run_id": "r-think",
    "agent": "think",
    "key": "file_type",
    "from": "docx",
    "to": "md",
}


def _propagation(payload, deliverable_run_id="r-act", agent="act"):
    from worker.tier2 import contract_propagation_check

    return contract_propagation_check(
        [dict(_VIOLATION)], deliverable_run_id, agent, payload
    )


def test_contract_propagation_param_match_propagated():
    results = _propagation('{"file_type": "md", "artifact_text": "hello"}')

    assert len(results) == 1
    assert results[0]["status"] == "propagated"
    assert results[0]["basis"] == "contract param match"
    assert results[0]["record"]["data"]["basis_kind"] == "param"
    assert results[0]["record"]["data"]["agent"] == "act"


def test_contract_propagation_param_match_corrected():
    results = _propagation('{"file_type": "docx", "artifact_text": "hello"}')

    assert len(results) == 1
    assert results[0]["status"] == "corrected"
    assert results[0]["basis"] == "contract param match"


def test_propagated_breach_claims_no_content_recovery_when_content_was_unmeasured():
    """A --no-judge run must not be told its content recovered.

    The propagated-breach note read "the run is recovered in content but shipped
    with a violated contract". That is a claim ABOUT CONTENT, and on a run where
    every node is unscored (no judge, no contracts) nothing measured it. Observed
    on the real CLI over testdata/topologies/fanout_join_echoes_breach.json.
    The propagation itself stays verified — it is read off the deliverable
    payload — but the aftermath must say UNKNOWN, not recovered.
    """
    from worker.tier2 import contract_propagation_check

    payload = '{"file_type": "md", "artifact_text": "hello"}'
    unmeasured = contract_propagation_check(
        [dict(_VIOLATION)], "r-act", "act", payload, content_measured=False
    )
    assert unmeasured[0]["status"] == "propagated"     # the breach still shipped
    assert "recovered in content" not in unmeasured[0]["note"]
    assert "never measured" in unmeasured[0]["note"]
    assert "UNKNOWN" in unmeasured[0]["note"]

    # When content DID run, the recovery reading is earned and stays.
    measured = contract_propagation_check(
        [dict(_VIOLATION)], "r-act", "act", payload, content_measured=True
    )
    assert "recovered in content" in measured[0]["note"]


def test_contract_propagation_path_extension_propagated_prose_does_not_flip():
    """No file_type param in the payload, but a structured artifact path ending
    '.md' proves the rewritten format shipped. The prose mentioning '.docx'
    (the ORIGINAL value) must NOT flip the verdict to corrected — only
    structured path values count, never free-text substring search."""
    results = _propagation(
        '{"artifact_path": "output/x.md", "artifact_text": "mentions .docx in prose"}'
    )

    assert len(results) == 1
    assert results[0]["status"] == "propagated"
    # Basis: the structured path extension, not a contract param.
    assert results[0]["record"]["data"] == {
        "key": "file_type", "from": "docx", "to": "md", "agent": "act",
        "status": "propagated", "basis_kind": "path",
        "path": "output/x.md", "ext": "md",
        # Whether the content channel measured anything on this run — the note's
        # aftermath clause turns on it (see the unmeasured-content test below).
        "content_measured": True,
    }


def test_contract_propagation_path_extension_corrected():
    results = _propagation('{"artifact_path": "output/x.docx"}')

    assert len(results) == 1
    assert results[0]["status"] == "corrected"
    assert results[0]["record"]["data"]["basis_kind"] == "path"
    assert results[0]["record"]["data"]["ext"] == "docx"


def test_contract_propagation_unverified_when_payload_missing_or_unparseable():
    for payload in (None, "not json at all", '{"other_key": true}'):
        results = _propagation(payload)
        assert len(results) == 1
        assert results[0]["status"] == "unverified"
        assert results[0]["basis"] == "nothing observable"
        assert results[0]["record"]["data"]["basis_kind"] == "none"

    # No deliverable run at all -> unverified even with a plausible payload.
    results = _propagation(
        '{"file_type": "md"}', deliverable_run_id=None, agent="unknown"
    )
    assert len(results) == 1
    assert results[0]["status"] == "unverified"


def test_contract_propagation_non_format_key_gets_no_path_evidence():
    """A 'lang' rewrite cannot be verified by a file extension — with the param
    absent the verdict must stay UNVERIFIED even if a path is present."""
    from worker.tier2 import contract_propagation_check

    violation = {"run_id": "r", "agent": "think", "key": "lang", "from": "cs", "to": "en"}
    results = contract_propagation_check(
        [violation], "r-act", "act", '{"artifact_path": "output/x.en"}'
    )
    assert len(results) == 1
    assert results[0]["status"] == "unverified"


def test_contract_propagation_dedupes_identical_violations():
    """Dedup keys off the typed record, not its rendered sentence — two identical
    violations are ONE fact however the template happens to word it."""
    from worker.tier2 import contract_propagation_check

    results = contract_propagation_check(
        [dict(_VIOLATION), dict(_VIOLATION)],
        "r-act",
        "act",
        '{"file_type": "md"}',
    )
    assert len(results) == 1


def test_contract_propagation_check_returns_structured_statuses():
    """The escalation decision keys off the structured status field — never off
    re-parsing the prose note."""
    from worker.tier2 import contract_propagation_check

    results = contract_propagation_check(
        [dict(_VIOLATION)],
        "r-render",
        "render",
        '{"artifact_path": "output/x.md", "artifact_text": "prose says .docx"}',
    )

    assert len(results) == 1
    r = results[0]
    assert r["status"] == "propagated"
    assert r["key"] == "file_type"
    assert r["from"] == "docx" and r["to"] == "md"
    assert r["record"]["data"]["basis_kind"] == "path"


def test_contract_propagation_check_corrected_and_unverified_statuses():
    from worker.tier2 import contract_propagation_check

    corrected = contract_propagation_check(
        [dict(_VIOLATION)], "r", "render", '{"artifact_path": "output/x.docx"}'
    )
    assert corrected[0]["status"] == "corrected"

    unverified = contract_propagation_check([dict(_VIOLATION)], "r", "render", None)
    assert unverified[0]["status"] == "unverified"


def test_classify_incident_escalates_shipped_latent_defect():
    """A verified shipped breach gets its OWN high-severity trigger — alerting
    must distinguish it from ordinary degraded_quality and from the low-priority
    degraded_recovered near-miss."""
    from worker.tier2 import classify_incident

    assert classify_incident("shipped_with_latent_defect", [], False) == (
        "latent_defect",
        "latent_defect",
    )
    # The un-escalated near-miss still routes to degraded_quality.
    assert classify_incident("degraded_recovered", [], False) == (
        "degraded_quality",
        "degraded_quality",
    )


def test_contract_recovery_from_propagation_not_successor_scores():
    """Lock (GREEN): the degraded_recovered -> shipped_with_latent_defect
    escalation is decided by the DETERMINISTIC contract_propagation check alone —
    never successor scores, never a judge.

    This guards the flagship deterministic verdict as the channel decoupling
    proceeds: contract "recovery" is defined as "the breach did NOT reach the
    shipped artifact" (a propagation fact), so a corrected/unverified breach must
    NOT escalate, and a non-recovered report type is left untouched. Fed by the
    real check so the whole chain (check -> escalation) is exercised."""
    from worker.tier2 import (
        contract_propagation_check,
        escalate_shipped_latent_defect,
    )

    # Breach VERIFIED in the shipped deliverable (file_type md shipped, docx asked).
    propagated = contract_propagation_check(
        [dict(_VIOLATION)], "r-render", "render", '{"artifact_path": "output/x.md"}'
    )
    assert propagated[0]["status"] == "propagated"
    effective, notes = escalate_shipped_latent_defect("degraded_recovered", propagated)
    assert effective == "shipped_with_latent_defect"
    assert notes and "VERIFIED in the shipped artifact" in notes[0]

    # Breach corrected before shipping -> the deliverable is conformant -> no escalation.
    corrected = contract_propagation_check(
        [dict(_VIOLATION)], "r-render", "render", '{"artifact_path": "output/x.docx"}'
    )
    assert corrected[0]["status"] == "corrected"
    assert escalate_shipped_latent_defect("degraded_recovered", corrected) == (
        "degraded_recovered",
        [],
    )

    # A non-recovered report type is never rewritten by this escalation, even
    # when the breach propagated — escalation only upgrades the near-miss verdict.
    assert escalate_shipped_latent_defect("cut_point", propagated) == ("cut_point", [])


def test_reanalysis_under_new_key_supersedes_prior_incident_end_to_end():
    """C2 through the full processor: analysing a graph opens incident K1; a
    re-analysis that reclassifies it (K2) supersedes the now-stale K1 instead of
    leaving both open — driven by tier2.process's own supersede_others=True."""
    from dataclasses import replace

    repo = _diamond_repo()
    processor, _streams = _make_processor(repo, judge=_diamond_judge())

    # First analysis: the fabrication cascade → degraded_quality incident (K1).
    asyncio.run(
        processor.process(
            Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key="k1")
        )
    )
    assert repo.incidents[(uid(1), "degraded_quality")]["status"] == "open"

    # Re-analysis reclassifies the SAME graph (a loop anomaly now flags it) →
    # loop_detected incident (K2, a different key). New dedup_key so the job
    # claim does not short-circuit the second run.
    repo.tier1[uid(1)] = replace(repo.tier1[uid(1)], flags=["loop_anomaly"])
    asyncio.run(
        processor.process(
            Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key="k2")
        )
    )

    # K1 is superseded, K2 is the sole open incident for the graph.
    assert repo.incidents[(uid(1), "degraded_quality")]["status"] == "superseded"
    assert repo.incidents[(uid(1), "loop_detected")]["status"] == "open"


def test_reclassified_analysis_supersedes_stale_incident():
    """Re-analysis that changes the incident class (e.g. degraded_quality ->
    latent_defect escalation) must not leave the old-class incident paging next
    to the new one: the latest completed analysis is authoritative."""
    import asyncio
    from uuid import uuid4

    from conftest import FakeRepo

    repo = FakeRepo()
    gid = uuid4()

    async def run() -> None:
        first = await repo.persist_tier2_result(
            dedup_key="k1", node_scores=[], graph_id=gid,
            incident_key="degraded_quality", incident_trigger="degraded_quality",
            blame=None, supersede_others=True,
        )
        second = await repo.persist_tier2_result(
            dedup_key="k2", node_scores=[], graph_id=gid,
            incident_key="latent_defect", incident_trigger="latent_defect",
            blame=None, supersede_others=True,
        )
        old = repo.incidents[(gid, "degraded_quality")]
        new = repo.incidents[(gid, "latent_defect")]
        assert old["id"] == first.incident_id and old["status"] == "superseded"
        assert new["id"] == second.incident_id and new["status"] == "open"

    asyncio.run(run())


def test_clean_reanalysis_supersedes_open_incident():
    """A re-analysis that comes back clean (no incident to open) supersedes the
    stale open incident instead of letting it page forever."""
    import asyncio
    from uuid import uuid4

    from conftest import FakeRepo

    repo = FakeRepo()
    gid = uuid4()

    async def run() -> None:
        await repo.persist_tier2_result(
            dedup_key="k1", node_scores=[], graph_id=gid,
            incident_key="degraded_quality", incident_trigger="degraded_quality",
            blame=None, supersede_others=True,
        )
        await repo.persist_tier2_result(
            dedup_key="k2", node_scores=[], graph_id=gid,
            incident_key=None, incident_trigger=None,
            blame=None, supersede_others=True,
        )
        assert repo.incidents[(gid, "degraded_quality")]["status"] == "superseded"

    asyncio.run(run())


def test_graph_not_found_path_does_not_supersede():
    """The bundle-missing early return persists with supersede_others left False
    — no analysis ran, so it must not touch existing incidents. Also: resolved
    incidents are history and stay untouched even by a real analysis."""
    import asyncio
    from uuid import uuid4

    from conftest import FakeRepo

    repo = FakeRepo()
    gid = uuid4()

    async def run() -> None:
        await repo.persist_tier2_result(
            dedup_key="k1", node_scores=[], graph_id=gid,
            incident_key="degraded_quality", incident_trigger="degraded_quality",
            blame=None, supersede_others=True,
        )
        # graph-not-found shape: no key, default supersede_others=False.
        await repo.persist_tier2_result(
            dedup_key="k2", node_scores=[], graph_id=gid,
            incident_key=None, incident_trigger=None, blame=None,
        )
        assert repo.incidents[(gid, "degraded_quality")]["status"] == "open"

        # A resolved incident is not resurrected/relabelled by a later analysis.
        repo.incidents[(gid, "degraded_quality")]["status"] = "resolved"
        await repo.persist_tier2_result(
            dedup_key="k3", node_scores=[], graph_id=gid,
            incident_key="latent_defect", incident_trigger="latent_defect",
            blame=None, supersede_others=True,
        )
        assert repo.incidents[(gid, "degraded_quality")]["status"] == "resolved"

    asyncio.run(run())


def test_reprocess_back_to_prior_key_reopens_superseded_incident():
    """A graph near a judge boundary can oscillate K1 -> K2 -> K1 across
    reprocesses. The third analysis reuses K1's incident row (is_new=False) —
    it must REOPEN it, not write a fresh authoritative report version onto a
    superseded incident nobody sees in the open list."""
    from dataclasses import replace

    repo = _diamond_repo()
    processor, _streams = _make_processor(repo, judge=_diamond_judge())

    # K1: the fabrication cascade -> degraded_quality.
    asyncio.run(
        processor.process(
            Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key="k1")
        )
    )
    original_flags = repo.tier1[uid(1)].flags
    assert repo.incidents[(uid(1), "degraded_quality")]["status"] == "open"

    # K2: a loop anomaly reclassifies the graph -> K1 superseded.
    repo.tier1[uid(1)] = replace(repo.tier1[uid(1)], flags=["loop_anomaly"])
    asyncio.run(
        processor.process(
            Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key="k2")
        )
    )
    assert repo.incidents[(uid(1), "degraded_quality")]["status"] == "superseded"

    # Back to K1: the flag clears and a reprocess lands on the original key.
    repo.tier1[uid(1)] = replace(repo.tier1[uid(1)], flags=original_flags)
    asyncio.run(
        processor.process(
            Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key="k3")
        )
    )
    assert repo.incidents[(uid(1), "degraded_quality")]["status"] == "open"
    assert repo.incidents[(uid(1), "loop_detected")]["status"] == "superseded"


def test_reused_incident_reopens_resolved_but_keeps_acknowledged():
    """Reuse of the SAME key reopens a resolved incident (a new authoritative
    analysis says the problem is live again), but never resets acknowledged —
    a human owns that state."""
    import asyncio
    from uuid import uuid4

    from conftest import FakeRepo

    repo = FakeRepo()
    gid = uuid4()

    async def run() -> None:
        await repo.persist_tier2_result(
            dedup_key="k1", node_scores=[], graph_id=gid,
            incident_key="degraded_quality", incident_trigger="degraded_quality",
            blame=None, supersede_others=True,
        )
        repo.incidents[(gid, "degraded_quality")]["status"] = "resolved"
        outcome = await repo.persist_tier2_result(
            dedup_key="k2", node_scores=[], graph_id=gid,
            incident_key="degraded_quality", incident_trigger="degraded_quality",
            blame=None, supersede_others=True,
        )
        assert outcome.is_new is False
        assert repo.incidents[(gid, "degraded_quality")]["status"] == "open"

        repo.incidents[(gid, "degraded_quality")]["status"] = "acknowledged"
        await repo.persist_tier2_result(
            dedup_key="k3", node_scores=[], graph_id=gid,
            incident_key="degraded_quality", incident_trigger="degraded_quality",
            blame=None, supersede_others=True,
        )
        assert repo.incidents[(gid, "degraded_quality")]["status"] == "acknowledged"

    asyncio.run(run())


def test_structured_claims_replace_llm_fact_propagation():
    """A JSON culprit output yields deterministic structured claims — the LLM
    claims prompt must NOT be consulted, entries carry source='structured', and
    a field absent from every checkable downstream payload emits a
    structured_field_drop signal."""

    class NoClaimsJudge(FakeJudge):
        async def complete_json(self, prompt, *, system=None):
            # The claims.md extraction prompt has a distinctive opener; the
            # per-node judge prompts legitimately mention the word "claims".
            assert "auditing one step of a multi-agent system for fact fabrication" not in prompt, (
                "LLM claims prompt must not run for structured JSON outputs"
            )
            return await super().complete_json(prompt, system=system)

    repo = _diamond_repo()
    processor, _streams = _make_processor(repo, judge=NoClaimsJudge(
        node_verdicts=_diamond_judge().node_verdicts
    ))
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    report = repo.blame_reports[0]
    fp = report["evidence"]["fact_propagation"]
    assert fp and fp[0]["source"] == "structured"
    assert fp[0]["claim"] == FAKE_PRICE
    assert set(fp[0]["found_in"]) == {str(uid(4)), str(uid(5))}
    # The fabricated price DID propagate, so no drop signal for it.
    drops = [
        s for s in report["evidence"]["deterministic_signals"]
        if s["name"] == "structured_field_drop"
    ]
    assert drops == []


def test_structured_field_drop_signal_when_value_vanishes():
    """A structured field that vanishes downstream — the deterministic
    replacement for fuzzy fact propagation names the exact dropped value."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", input_inline="make an offer", end_time=1.0),
                make_run(2, "pricer", output_inline='{"unit_price": "1234.56 CZK"}',
                         end_time=2.0),
                make_run(3, "writer", output_inline="a final offer text without the number",
                         end_time=3.0),
            ],
            [(1, 2), (2, 3)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.2,
        terminal_judge_reasoning="price missing from the offer",
        flags=[],
        flagged=True,
        sampled=False,
    )
    judge = FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "pricer": {"task_score": 0.2, "input_flawed": False, "reasoning": "bad"},
            "writer": {"task_score": 0.9, "input_flawed": True, "reasoning": "ok"},
        }
    )
    processor, _streams = _make_processor(repo, judge=judge)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    report = repo.blame_reports[0]
    drops = [
        s for s in report["evidence"]["deterministic_signals"]
        if s["name"] == "structured_field_drop"
    ]
    assert drops, "the vanished unit_price must raise a drop signal"
    assert any("1234.56 CZK" in s["detail"] for s in drops)
    assert all(s["provenance"] == "deterministic" for s in drops)
    assert all(s["scope"] == "propagation" for s in drops)


def test_two_independent_faults_terminal_axes_and_required_checklist():
    """Round-6 fixes together: content-bad terminal + verified format breach =
    TWO independent faults (no false corroboration, no 'ok in CONTENT only'
    over a bad content verdict); decided_by names the deterministic tier;
    required-facts checklist leads fact propagation with the negative finding."""
    from worker.types import CheckRule

    repo = FakeRepo()
    repo.check_rules = [
        CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                  spec={"name": "budget table", "match": "word_prefix", "pattern": "rozpoč"})
    ]
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", input_inline='{"request": "x", "file_type": "docx"}',
                         end_time=1.0),
                make_run(2, "think", input_inline='{"file_type": "docx"}',
                         output_inline='{"file_type": "md", "outline": ["a"]}', end_time=2.0),
                make_run(3, "render", input_inline='{"file_type": "md"}',
                         output_inline='{"artifact_path": "out/x.md", "artifact_text": "no budget here"}',
                         end_time=3.0),
            ],
            [(1, 2), (2, 3)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.0,
        terminal_judge_reasoning=(
            "deterministic deliverable check failure: required section "
            "'budget table' not found"
        ),
        flags=["required_section_missing"],
        flagged=True,
        sampled=False,
    )
    judge = FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "think": {"task_score": 0.9, "input_flawed": False, "reasoning": "fine plan"},
            "render": {"task_score": 0.9, "input_flawed": True, "reasoning": "ok"},
        }
    )
    processor, _streams = _make_processor(repo, judge=judge)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    report = repo.blame_reports[0]
    ev = report["evidence"]
    tv = ev["terminal_verdict"]
    # (9) provenance of the terminal decision
    assert tv["decided_by"] == "deterministic"
    # (1) two axes, no self-contradicting template
    assert tv["contract_conformance"].startswith("nonconformant (verified)")
    assert "TWO independent faults" in tv["caveat"]
    assert "ok in CONTENT only" not in tv["caveat"]
    # (2) no false corroboration: the reasoning does not cite the breach
    notes = " ".join(ev["notes"])
    assert "TWO INDEPENDENT faults" in notes
    assert "the breach and the terminal failure agree" not in notes
    # (6) required-facts checklist leads fact propagation with the negative
    fp = ev["fact_propagation"]
    assert fp[0]["source"] == "required"
    assert fp[0]["found_in"] == []          # rozpoč* nowhere in producers
    assert fp[0]["checked"] >= 1
    # (4) R4 / channel decoupling: producers get NO claimed→effective override.
    # think's judged score is untouched; the contract breach travels as its own
    # deterministic evidence stream (making think a deterministic culprit), not a
    # struck-through sentinel. The override vehicle is verifier-only now.
    assert not any(o["run_id"] == str(uid(2)) for o in ev["score_overrides"])
    assert repo.node_scores[uid(2)].quality_score > 0.5
    contract_keys = {(c["run_id"], c["key"]) for c in ev["contract_violations"]}
    assert (str(uid(2)), "file_type") in contract_keys


def test_stale_deterministic_terminal_is_not_ground_truth():
    """Reconciliation: tier1 said 'bad — required section missing' under an old
    rule set; the current rules no longer reproduce that basis (rule deleted or
    changed). The stored verdict is STALE — checkable=False, never drives blame,
    and the report says why (the self-contradiction fix: terminal claiming a
    section missing while the checklist shows it present)."""
    repo = FakeRepo()
    repo.check_rules = []  # the rule tier1 fired under no longer exists
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", end_time=1.0),
                make_run(2, "writer", output_inline="a healthy document text",
                         end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.0,
        terminal_judge_reasoning=(
            "deterministic deliverable check failure: required section "
            "'budget table' not found"
        ),
        flags=["required_section_missing"],
        flagged=True,
        sampled=False,
    )
    judge = FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "writer": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"},
        }
    )
    processor, _streams = _make_processor(repo, judge=judge)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    # No blame report may be built on the stale bad terminal: with healthy
    # scores and no reproducible failure there is nothing to open.
    if repo.blame_reports:
        ev = repo.blame_reports[0]["evidence"]
        tv = ev["terminal_verdict"]
        assert tv["stale"] is True
        assert tv["checkable"] is False
        notes = " ".join(ev["notes"])
        assert "terminal_stale" in notes
        assert "no longer reproduces" in notes
        # And no verdict may treat the stale bad terminal as ground truth.
        assert repo.blame_reports[0]["report_type"] not in (
            "composition_failure",
        )


def test_reproducible_deterministic_terminal_stays_ground_truth():
    """When the current rules STILL fail the deliverable, the verdict is not
    stale — reconciliation only fires on divergence."""
    from worker.types import CheckRule

    repo = FakeRepo()
    repo.check_rules = [
        CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                  spec={"name": "budget table", "match": "word_prefix", "pattern": "rozpoč"})
    ]
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", end_time=1.0),
                make_run(2, "writer", output_inline="document without the section",
                         end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.0,
        terminal_judge_reasoning=(
            "deterministic deliverable check failure: required section "
            "'budget table' not found"
        ),
        flags=["required_section_missing"],
        flagged=True,
        sampled=False,
    )
    judge = FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "writer": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"},
        }
    )
    processor, _streams = _make_processor(repo, judge=judge)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    assert repo.blame_reports
    tv = repo.blame_reports[0]["evidence"]["terminal_verdict"]
    assert tv["stale"] is False
    assert tv["checkable"] is True


def _stale_repo(rules, stored_hash):
    """Graph whose stored tier1 verdict (required-section bad) will NOT
    reproduce under `rules` — used to test the stale-cause provenance."""
    from worker.types import CheckRule  # noqa: F401  (callers build rules)

    repo = FakeRepo()
    repo.check_rules = rules
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", end_time=1.0),
                make_run(2, "writer", output_inline="a healthy document with rozpočet",
                         end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.0,
        terminal_judge_reasoning=(
            "deterministic deliverable check failure: required section "
            "'budget table' not found"
        ),
        flags=["required_section_missing"],
        flagged=True,
        sampled=False,
        check_rules_hash=stored_hash,
    )
    return repo


def _judge_ok():
    # writer scores LOW so a cut_point report exists and the stale-terminal
    # evidence has somewhere to persist — otherwise these tests pass vacuously.
    return FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "writer": {"task_score": 0.2, "input_flawed": False, "reasoning": "weak"},
        }
    )


def _stale_cause(repo):
    processor, _s = _make_processor(repo, judge=_judge_ok())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))
    if not repo.blame_reports:
        return None
    return repo.blame_reports[0]["evidence"]["terminal_verdict"].get("stale_cause")


def test_stale_cause_rule_change_detected_by_fingerprint():
    """Stored fingerprint differs from the current one -> the cause is a RULE
    CHANGE, stated with both fingerprints — not artifact divergence."""
    from worker.signals import check_rules_fingerprint
    from worker.types import CheckRule

    # Current rules: none (the old rule was deleted) -> basis won't reproduce.
    repo = _stale_repo([], stored_hash="aaaabbbbcccc")
    cause = _stale_cause(repo)
    assert cause is not None
    assert "rule set CHANGED" in cause
    assert "aaaabbbbcccc" in cause


def test_stale_cause_divergence_when_fingerprint_unchanged():
    """Same fingerprint but the payload now PASSES the rule -> the artifact/
    payload itself diverged (representation divergence)."""
    from worker.signals import check_rules_fingerprint
    from worker.types import CheckRule

    rule = CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                     spec={"name": "budget table", "match": "word_prefix", "pattern": "rozpoč"})
    settings = make_settings()
    current_fp = check_rules_fingerprint([rule], min_artifact_bytes=settings.min_artifact_bytes)
    # Stored hash == current hash; the writer payload CONTAINS rozpočet, so the
    # stored "missing" verdict cannot reproduce -> divergence branch.
    repo = _stale_repo([rule], stored_hash=current_fp)
    cause = _stale_cause(repo)
    assert cause is not None
    assert "UNCHANGED" in cause
    assert "representation divergence" in cause


def test_stale_cause_unknown_for_prestamping_verdicts():
    repo = _stale_repo([], stored_hash=None)
    cause = _stale_cause(repo)
    assert cause is not None
    assert "predates rule-set stamping" in cause


def test_stale_plus_shipped_caveat_does_not_claim_ok_content():
    """stale × shipped template collision: the caveat must not assert 'ok in
    CONTENT only' about a content verdict that was just discarded as stale."""
    from worker.signals import check_rules_fingerprint  # noqa: F401
    from worker.types import CheckRule  # noqa: F401

    repo = FakeRepo()
    repo.check_rules = []  # stored rule gone -> stale
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", input_inline='{"file_type": "docx"}', end_time=1.0),
                make_run(2, "think", input_inline='{"file_type": "docx"}',
                         output_inline='{"file_type": "md", "outline": ["a"]}', end_time=2.0),
                make_run(3, "render", input_inline='{"file_type": "md"}',
                         output_inline='{"artifact_path": "out/x.md", "artifact_text": "body"}',
                         end_time=3.0),
            ],
            [(1, 2), (2, 3)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.0,
        terminal_judge_reasoning="deterministic deliverable check failure: required section missing",
        flags=["required_section_missing"],
        flagged=True,
        sampled=False,
        check_rules_hash="aaaabbbbcccc",
    )
    judge = FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "think": {"task_score": 0.9, "input_flawed": False, "reasoning": "plan ok"},
            "render": {"task_score": 0.9, "input_flawed": True, "reasoning": "ok"},
        }
    )
    processor, _s = _make_processor(repo, judge=judge)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    assert repo.blame_reports
    ev = repo.blame_reports[0]["evidence"]
    tv = ev["terminal_verdict"]
    assert tv["stale"] is True
    assert tv["contract_conformance"].startswith("nonconformant (verified)")
    assert "ok in CONTENT only" not in (tv.get("caveat") or "")
    assert "STALE" in tv["caveat"]
    # And no manufactured manifestation on a discarded failure claim.
    assert ev["manifestation_run_ids"] == []


def test_escalation_rewrites_candidacy_narrative():
    """#2: the engine wrote 'a near-miss, not the origin of a live failure'
    BEFORE the propagation check proved the breach shipped. After escalation the
    candidacy must carry the escalated narrative — the same document must not
    contain a sentence and its literal negation."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orchestrator", input_inline='{"file_type": "docx"}', end_time=1.0),
                make_run(2, "think", input_inline='{"file_type": "docx"}',
                         output_inline='{"file_type": "md", "outline": ["a"]}', end_time=2.0),
                make_run(3, "render", input_inline='{"file_type": "md"}',
                         output_inline='{"artifact_path": "out/x.md", "artifact_text": "body"}',
                         end_time=3.0),
            ],
            [(1, 2), (2, 3)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="ok",
        terminal_judge_score=1.0,
        terminal_judge_reasoning="content looks complete",
        flags=[],
        flagged=True,
        sampled=False,
        check_rules_hash="944ee758f3a5",
    )
    judge = FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "think": {"task_score": 0.9, "input_flawed": False, "reasoning": "plan ok"},
            "render": {"task_score": 0.9, "input_flawed": True, "reasoning": "ok"},
        }
    )
    processor, _s = _make_processor(repo, judge=judge)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    assert repo.blame_reports
    report = repo.blame_reports[0]
    assert report["report_type"] == "shipped_with_latent_defect"
    ev = report["evidence"]
    think_candidacy = ev["candidacy"][str(uid(2))]
    assert "origin (escalated)" in think_candidacy
    assert "silent failure" in think_candidacy
    assert "near-miss (fragile node), not the origin" not in think_candidacy
    # P4 / channel decoupling: think scored 0.9 (>= threshold 0.5) — it has NO
    # content degradation. The old 60% content_degradation row was a SENTINEL
    # ARTIFACT: the floor dropped think to 0.10, faking a drop from orchestrator
    # 1.0. With the floor gone the honest inventory is exactly ONE defect — the
    # deterministically observed contract breach (via="deterministic").
    breakdown = {d["defect"]: d["attribution"] for d in ev["attribution_breakdown"]}
    assert breakdown["contract_violation"] == 0.95
    assert "content_degradation" not in breakdown
    assert list(breakdown) == ["contract_violation"]
    # Headline attribution = the verdict-carrying defect's attribution. The
    # verdict (shipped_with_latent_defect) is carried by the contract breach,
    # so the headline matches the contract entry — never a blended ceiling.
    assert ev["attribution_confidence"] == breakdown["contract_violation"]
    # The engine's near-miss finding is marked superseded with the SAME
    # structured mechanism the report applies to refuted judge assessments —
    # no double standard between LLM claims and our own.
    superseded = {s["slug"]: s for s in ev["superseded_notes"]}
    assert superseded["degraded_recovered"]["superseded_by"] == "escalation"
    assert "refuted" in superseded["degraded_recovered"]["reason"]
    # The original note itself stays in the ledger (history, not erasure).
    assert any(n.startswith("degraded_recovered:") for n in ev["notes"])


def test_seeded_cost_rule_records_shadow_decision_not_reasoning_note():
    """Shadow policy gate on the diamond: a {"cost_over": 0.0001} block rule
    fires -> policy_decisions row recorded. Governance decisions are a
    DIFFERENT category of claim from causal findings: they must NOT be mixed
    into the report's reasoning notes — the audit table (and its own UI panel)
    is their only home."""
    from dataclasses import replace

    from worker.types import PolicyRule

    repo = _diamond_repo()
    repo.add_bundle(replace(repo.bundles[uid(1)], total_cost_usd=0.0041))
    repo.policy_rules = [
        PolicyRule(
            id=1,
            name="cost-cap",
            predicate={"cost_over": 0.0001},
            action="block",
            shadow=True,
            enabled=True,
        )
    ]
    processor, _streams = _make_processor(repo, judge=_diamond_judge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    assert len(repo.policy_decisions) == 1
    decision = repo.policy_decisions[0]
    assert decision["graph_id"] == uid(1)
    assert decision["rule_name"] == "cost-cap"
    assert decision["decision"] == "would_block"
    assert decision["mode"] == "shadow"
    assert decision["detail"] == "cost_over: 0.0041 > 0.0001"

    notes = repo.blame_reports[0]["evidence"]["notes"]
    assert not any(n.startswith("policy_shadow:") for n in notes)
    assert not any("was blocked" in n for n in notes)


def test_no_policy_rules_means_no_decisions():
    repo = _diamond_repo()
    processor, _streams = _make_processor(repo, judge=_diamond_judge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))
    assert repo.policy_decisions == []
    assert not any(
        n.startswith("policy_shadow:")
        for n in repo.blame_reports[0]["evidence"]["notes"]
    )


def test_blame_report_is_stamped_with_judge_prompt_hash():
    """Calibration slicing (roadmap 2.7): the blame report records the
    worker's OWN judge-prompt fingerprint (12 hex; the judge MODEL is not
    recorded — known limitation)."""
    import re

    from worker.policy import judge_prompts_fingerprint

    repo = _diamond_repo()
    processor, _streams = _make_processor(repo, judge=_diamond_judge())
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    stamped = repo.blame_reports[0]["judge_prompt_hash"]
    assert stamped == judge_prompts_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{12}", stamped)


# ---- Terminal rubric split (content vs form) -----------------------------------


def test_classify_incident_terminal_defect_unlocalized_is_quality():
    assert classify_incident("terminal_defect_unlocalized", [], True) == (
        "degraded_quality",
        "degraded_quality",
    )


def test_terminal_form_dimension_flows_into_report_evidence():
    """A content-ok terminal with a bad FORM dimension: the split must reach
    the report — evidence.terminal_verdict carries the form dict and the
    engine emits the design-level form_defect_shipped note (no verification
    gap is manufactured, the content is fine)."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "writer", output_inline="markdown report", end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    repo.tier1[uid(1)] = Tier1Verdict(
        graph_id=uid(1),
        terminal_judge_verdict="ok",
        terminal_judge_score=1.0,
        terminal_judge_reasoning="content complete",
        terminal_form={
            "verdict": "bad",
            "requirement": "jako PDF",
            "observed": "markdown text",
            "reasoning": "markdown shipped where PDF was requested",
        },
        flags=["terminal_form_breach"],
        flagged=True,
        sampled=False,
    )
    processor, _ = _make_processor(repo)
    msg = Tier2Message(graph_id=str(uid(1)), trigger="tier1", dedup_key=str(uid(1)))
    asyncio.run(processor.process(msg))

    report = repo.blame_reports[0]
    tv = report["evidence"]["terminal_verdict"]
    assert tv["bad"] is False  # content dimension untouched by the form miss
    assert tv["form"]["bad"] is True
    assert tv["form"]["requirement"] == "jako PDF"
    notes = report["evidence"]["notes"]
    assert any(n.startswith("form_defect_shipped:") for n in notes)
    assert not any(n.startswith("verification_gap:") for n in notes)


# ---- §2.4 reconcile: required-section representation divergence -----------------


def test_required_fact_divergence_producer_has_section_deliverable_missing():
    """The report-#1 class: a required section present in a producer's payload
    but ABSENT from the shipped deliverable must surface a
    representation_divergence with refs into the REAL findings list. The worker
    only emits the per-representation side findings; the ENGINE's reconcile
    produces the divergence — a worker-local reconcile used to ship a
    divergence whose finding_refs indexed a throwaway list."""
    from blame_engine import reconcile
    from worker.types import CheckRule
    from worker.tier2 import required_section_findings

    rule = CheckRule(
        id=1, agent_name=None, graph_type=None, kind="required_section",
        spec={"name": "budget table", "match": "word_prefix", "pattern": "rozpoč"},
    )
    bundle = make_bundle(
        [
            make_run(1, "orchestrator", end_time=1.0),
            make_run(2, "act", output_inline="obsah: rozpočet 125 000", end_time=2.0),
            make_run(3, "render", output_inline="no budget section here", end_time=3.0),
        ],
        [(1, 2), (2, 3)],
    )
    payloads = {
        uid(2): (None, "obsah: rozpočet 125 000"),  # producer HAS the section
        uid(3): (None, "no budget section here"),    # deliverable does NOT
    }
    sides = required_section_findings([rule], bundle, payloads, "no budget section here")

    assert [f.subject for f in sides] == ["producers", "deliverable"]
    assert [f.data["value"] for f in sides] == ["present", "absent"]
    assert all(f.fact_key == "required_section:budget table" for f in sides)

    divergences = reconcile(sides)
    assert len(divergences) == 1
    d = divergences[0]
    assert d.kind == "representation_divergence"
    assert d.fact_key == "required_section:budget table"
    assert set(d.data["values"]) == {"present", "absent"}
    # The refs point at the actual side findings, and both sides EXIST.
    assert d.data["finding_refs"] == [0, 1]


def test_required_fact_no_divergence_when_both_agree():
    """Present in producer AND deliverable → both sides emitted, no divergence."""
    from blame_engine import reconcile
    from worker.types import CheckRule
    from worker.tier2 import required_section_findings

    rule = CheckRule(
        id=1, agent_name=None, graph_type=None, kind="required_section",
        spec={"name": "budget table", "match": "word_prefix", "pattern": "rozpoč"},
    )
    bundle = make_bundle(
        [
            make_run(1, "orchestrator", end_time=1.0),
            make_run(2, "render", output_inline="rozpočet 125 000", end_time=2.0),
        ],
        [(1, 2)],
    )
    payloads = {uid(2): (None, "rozpočet 125 000")}
    sides = required_section_findings([rule], bundle, payloads, "rozpočet 125 000")
    assert [f.data["value"] for f in sides] == ["present", "present"]
    assert reconcile(sides) == []
