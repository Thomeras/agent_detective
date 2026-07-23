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


class ExplodingJudge:
    """Judge that must never be reached (AssertionError is not caught by the
    judge retry wrapper, so an unexpected call fails the test loudly)."""

    async def complete_json(self, prompt, *, system=None):
        raise AssertionError("LLM judge must not be called on a deterministic artifact integrity failure")


def test_artifact_integrity_failure_is_deterministic_and_skips_the_judge():
    # The deliverable declares report.docx but magic bytes say plain text: the
    # verdict is ground-truth bad at score 0.0 with zero LLM calls. The meta
    # comes from the OUT-OF-BAND run attribute, never the payload.
    meta = (
        '[{"path": "out/report.docx", "size": 5000, "declared_ext": "docx",'
        ' "detected_kind": "text", "parse_ok": true, "nonempty": true}]'
    )
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "writer", output_inline="wrote the report",
                         end_time=2.0, artifact_meta=meta),
            ],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo, judge=ExplodingJudge())
    verdict = _verdict(repo)
    assert "artifact_integrity" in verdict.flags
    assert verdict.terminal_judge_verdict == "bad"
    assert verdict.terminal_judge_score == 0.0
    assert verdict.terminal_judge_reasoning.startswith(
        "deterministic deliverable check failure: declared .docx but content is text"
    )
    assert verdict.flagged is True
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_healthy_artifact_meta_does_not_flag_and_judge_runs():
    meta = (
        '[{"path": "out/notes.md", "size": 5000, "declared_ext": "md",'
        ' "detected_kind": "text", "parse_ok": true, "nonempty": true}]'
    )
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "writer", output_inline="wrote the notes",
                         end_time=2.0, artifact_meta=meta),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge()
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert "artifact_integrity" not in verdict.flags
    assert "terminal" in judge.calls  # ordinary flow: the judge did run
    assert verdict.terminal_judge_verdict == "ok"


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


def test_registered_required_section_missing_is_deterministic_bad():
    """A registered requirement physically absent from the deliverable text is
    ground truth — verdict bad 0.0, judge skipped ('budget table is missing'
    without an LLM)."""
    from worker.types import CheckRule

    repo = FakeRepo()
    repo.check_rules = [
        CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                  spec={"name": "budget table", "match": "substring", "pattern": "rozpočet"})
    ]
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"),
             make_run(2, "writer", output_inline="an overview without the required part",
                      end_time=2.0)],
            [(1, 2)],
        )
    )
    run_tier1(repo, judge=ExplodingJudge())
    verdict = _verdict(repo)
    assert "required_section_missing" in verdict.flags
    assert verdict.terminal_judge_verdict == "bad"
    assert verdict.terminal_judge_score == 0.0
    assert "budget table" in verdict.terminal_judge_reasoning
    assert verdict.flagged is True


def test_soft_flags_are_recorded_but_do_not_page():
    """A contact email in the deliverable is an observation, not an incident:
    the soft flag rides in the verdict but flagged stays False."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"),
             make_run(2, "writer", output_inline="contact us at info@example.com",
                      end_time=2.0)],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo)
    verdict = _verdict(repo)
    assert "sensitive_data_exposure" in verdict.flags   # recorded
    assert verdict.flagged is False                     # does not page
    assert streams.messages(STREAM_GRAPHS_TIER2) == []


def test_duplicate_side_effect_is_hard_and_pages():
    calls = (
        '[{"name": "send_email", "args_sha": "abc123def456", "status": "ok"},'
        ' {"name": "send_email", "args_sha": "abc123def456", "status": "ok"}]'
    )
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"),
             make_run(2, "mailer", output_inline="sent", end_time=2.0,
                      tool_calls=calls)],
            [(1, 2)],
        )
    )
    streams = run_tier1(repo)
    verdict = _verdict(repo)
    assert "duplicate_side_effect" in verdict.flags
    assert verdict.flagged is True
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_tier1_feeds_rolling_baselines():
    """The Welford writer: every processed run folds tokens/cost samples into
    agent_stats — the baseline the cost/token anomaly check reads."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch", tokens_out=10, cost_usd=0.01),
             make_run(2, "writer", tokens_out=100, cost_usd=0.05, end_time=2.0)],
            [(1, 2)],
        )
    )
    run_tier1(repo)
    assert repo.agent_stats["writer"].sample_count == 1
    assert repo.agent_stats["writer"].tokens_out_mean == 100.0
    assert repo.agent_stats["writer"].cost_mean == 0.05
    assert repo.agent_stats["orch"].sample_count == 1


def test_tier1_verdict_is_stamped_with_judge_prompt_hash():
    """Calibration slicing (roadmap 2.7): every verdict records the worker's
    OWN judge-prompt fingerprint (12 hex; the judge MODEL is not recorded —
    known limitation)."""
    import re

    from worker.policy import judge_prompts_fingerprint

    repo = FakeRepo()
    repo.add_bundle(
        make_bundle([make_run(1, "orch"), make_run(2, "writer", end_time=2.0)], [(1, 2)])
    )
    run_tier1(repo)
    verdict = _verdict(repo)
    assert verdict.judge_prompt_hash == judge_prompts_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{12}", verdict.judge_prompt_hash)
