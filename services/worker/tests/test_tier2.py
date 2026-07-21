"""Tier 2: idempotence, the flagship silent-hallucination diamond, and the
sampled-unclassified rule (no incident for healthy sampled graphs)."""

import asyncio

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
