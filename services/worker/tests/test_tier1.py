"""Tier 1: deterministic flags, one terminal judge call, tier2 publish/sampling."""

import asyncio

from worker.tier1 import Tier1Processor
from worker.types import STREAM_GRAPHS_TIER2, OutputContract

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


def run_tier1(repo, judge=None, settings=None, store=None):
    streams = FakeStreams()
    judge = judge or FakeJudge()
    settings = settings or make_settings()
    processor = Tier1Processor(repo, store or FakeObjectStore(), streams, judge, settings)
    graph_id = str(next(iter(repo.bundles)))
    asyncio.run(processor.process(graph_id))
    return streams


def _verdict(repo):
    return next(iter(repo.tier1.values()))


def test_failed_run_is_flagged():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "worker", status="failed", end_time=2.0)],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo)
    verdict = _verdict(repo)
    assert "failed_runs" in verdict.flags
    assert verdict.flagged is True
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_cost_overrun_is_flagged():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "worker", cost_usd=9.0, end_time=2.0)],
            [(1, 2)],
            total_cost_usd=9.0,
        )
    )
    streams = run_tier1(repo, settings=make_settings(cost_budget_default_usd=1.0))
    assert "cost_overrun" in _verdict(repo).flags
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_loop_anomaly_is_flagged():
    repo = FakeRepo()
    # 3-node cycle: iterations 3 > max_loop_iterations 2.
    repo.add_bundle(
        make_bundle(
            [make_run(1, "a"), make_run(2, "a"), make_run(3, "a")],
            [(1, 2), (2, 3), (3, 1)],
        )
    )
    streams = run_tier1(repo, settings=make_settings(max_loop_iterations=2))
    assert "loop_anomaly" in _verdict(repo).flags
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_schema_violation_is_flagged():
    repo = FakeRepo()
    repo.contracts = [
        OutputContract(
            agent_name="worker",
            agent_version_pattern=None,
            json_schema={"type": "object", "required": ["price"]},
        )
    ]
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "worker", output_inline='{"nope": 1}', end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo)
    assert "schema_violation" in _verdict(repo).flags
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_degenerate_terminal_output_is_flagged():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "worker", output_inline="   ", end_time=2.0)],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo)
    assert "degenerate_output" in _verdict(repo).flags


def test_silent_hallucination_caught_by_terminal_judge_despite_status_ok():
    # Every run reports ok and no deterministic flag fires, but the terminal
    # judge returns "bad" -> the graph is still flagged for tier2.
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "worker", output_inline="confident but wrong", end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(terminal={"verdict": "bad", "score": 0.15, "reasoning": "hallucinated"})
    streams = run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.flags == []  # no deterministic flag
    assert verdict.terminal_judge_verdict == "bad"
    assert verdict.flagged is True
    messages = streams.messages(STREAM_GRAPHS_TIER2)
    assert len(messages) == 1
    assert messages[0]["trigger"] == "tier1"
    assert messages[0]["dedup_key"] == str(uid(1))


def test_healthy_graph_not_flagged_and_not_sampled_by_default():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "worker", end_time=2.0)],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo)  # tier2_sample_pct default 0
    verdict = _verdict(repo)
    assert verdict.flagged is False
    assert verdict.sampled is False
    assert streams.messages(STREAM_GRAPHS_TIER2) == []


def test_healthy_graph_sampled_when_pct_100():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "worker", end_time=2.0)],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo, settings=make_settings(tier2_sample_pct=100))
    verdict = _verdict(repo)
    assert verdict.flagged is False
    assert verdict.sampled is True
    messages = streams.messages(STREAM_GRAPHS_TIER2)
    assert len(messages) == 1
    assert messages[0]["trigger"] == "sampled"
