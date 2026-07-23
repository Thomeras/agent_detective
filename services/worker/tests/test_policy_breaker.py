"""Policy evaluator DSL matrix, judge-prompt fingerprint, and the tier2
circuit breaker (records a decision — nothing is enforced)."""

import asyncio
import hashlib
import re
from pathlib import Path

from worker.policy import (
    STREAM_CONTROL_SIGNALS,
    evaluate_policies,
    judge_prompts_fingerprint,
)
from worker.tier2 import Tier2Processor
from worker.types import PolicyRule, Tier1Verdict, Tier2Message

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


def _rule(predicate, *, action="warn", name="r1", enabled=True):
    return PolicyRule(
        id=1, name=name, predicate=predicate, action=action, shadow=True, enabled=enabled
    )


def _evaluate(rules, **overrides):
    kwargs = dict(
        flags=[],
        signal_names=[],
        report_type=None,
        graph_cost=None,
        min_node_score=None,
    )
    kwargs.update(overrides)
    return evaluate_policies(rules, **kwargs)


# --- DSL condition matrix -------------------------------------------------


def test_flags_any_fires_on_present_flag_only():
    rules = [_rule({"flags_any": ["failed_runs", "loop_anomaly"]})]
    hits = _evaluate(rules, flags=["loop_anomaly"])
    assert len(hits) == 1
    assert hits[0].decision == "would_warn"
    assert "flags_any" in hits[0].detail and "loop_anomaly" in hits[0].detail
    assert _evaluate(rules, flags=["cost_overrun"]) == []


def test_signals_any_fires_on_fired_signal_only():
    rules = [_rule({"signals_any": ["artifact_integrity_fail"]})]
    hits = _evaluate(rules, signal_names=["artifact_integrity_fail"])
    assert len(hits) == 1
    assert "signals_any" in hits[0].detail
    assert "artifact_integrity_fail" in hits[0].detail
    assert _evaluate(rules, signal_names=["structured_field_drop"]) == []


def test_report_types_any_matches_effective_report_type():
    rules = [_rule({"report_types_any": ["cut_point", "loop_detected"]})]
    hits = _evaluate(rules, report_type="cut_point")
    assert len(hits) == 1
    assert "report_types_any" in hits[0].detail and "cut_point" in hits[0].detail
    assert _evaluate(rules, report_type="degraded_recovered") == []
    assert _evaluate(rules, report_type=None) == []


def test_cost_over_fires_strictly_above_threshold_with_observed_value():
    rules = [_rule({"cost_over": 0.001}, action="block")]
    hits = _evaluate(rules, graph_cost=0.0041)
    assert len(hits) == 1
    assert hits[0].decision == "would_block"
    assert hits[0].detail == "cost_over: 0.0041 > 0.001"
    assert _evaluate(rules, graph_cost=0.001) == []  # not strictly over
    assert _evaluate(rules, graph_cost=None) == []  # unknown cost: no claim


def test_score_below_fires_on_any_node_below_threshold():
    rules = [_rule({"score_below": 0.5})]
    hits = _evaluate(rules, min_node_score=0.1)
    assert len(hits) == 1
    assert "score_below" in hits[0].detail and "0.1" in hits[0].detail
    assert _evaluate(rules, min_node_score=0.5) == []  # not strictly below
    assert _evaluate(rules, min_node_score=None) == []  # nothing scored


def test_any_semantics_one_matching_condition_fires_once_naming_it():
    rules = [_rule({"flags_any": ["failed_runs"], "cost_over": 100.0})]
    hits = _evaluate(rules, flags=["failed_runs"], graph_cost=0.01)
    assert len(hits) == 1
    assert "flags_any" in hits[0].detail
    assert "cost_over" not in hits[0].detail  # only the FIRING condition is named


def test_multiple_matching_conditions_are_all_named_in_detail():
    rules = [_rule({"flags_any": ["failed_runs"], "cost_over": 0.001})]
    hits = _evaluate(rules, flags=["failed_runs"], graph_cost=0.5)
    assert len(hits) == 1
    assert "flags_any" in hits[0].detail and "cost_over" in hits[0].detail


def test_action_maps_to_would_block_or_would_warn():
    warn = _rule({"flags_any": ["failed_runs"]}, action="warn", name="w")
    block = _rule({"flags_any": ["failed_runs"]}, action="block", name="b")
    hits = _evaluate([warn, block], flags=["failed_runs"])
    assert [(h.rule_name, h.decision) for h in hits] == [
        ("w", "would_warn"),
        ("b", "would_block"),
    ]


def test_malformed_predicates_are_skipped_silently():
    # Non-dict predicate: whole rule skipped.
    assert _evaluate([_rule(["not", "a", "dict"])], flags=["failed_runs"]) == []
    # Wrong-typed condition values: that condition never fires.
    assert _evaluate([_rule({"cost_over": "cheap"})], graph_cost=9.9) == []
    assert _evaluate([_rule({"flags_any": "failed_runs"})], flags=["failed_runs"]) == []
    assert _evaluate([_rule({"score_below": True})], min_node_score=0.0) == []
    # ...but well-formed conditions in the same rule still evaluate.
    mixed = _rule({"cost_over": "cheap", "flags_any": ["failed_runs"]})
    hits = _evaluate([mixed], flags=["failed_runs"], graph_cost=9.9)
    assert len(hits) == 1 and "flags_any" in hits[0].detail


def test_empty_predicate_and_disabled_rule_never_fire():
    assert _evaluate([_rule({})], flags=["failed_runs"], graph_cost=9.9) == []
    disabled = _rule({"flags_any": ["failed_runs"]}, enabled=False)
    assert _evaluate([disabled], flags=["failed_runs"]) == []


# --- judge prompt fingerprint ---------------------------------------------


def test_judge_prompts_fingerprint_is_12_hex_over_sorted_prompt_bytes():
    fp = judge_prompts_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{12}", fp)
    prompts_dir = (
        Path(__file__).resolve().parent.parent / "worker" / "prompts"
    )
    digest = hashlib.sha256()
    for path in sorted(prompts_dir.glob("*.md"), key=lambda p: p.name):
        digest.update(path.read_bytes())
    assert fp == digest.hexdigest()[:12]
    # Cached: stable across calls within one process.
    assert judge_prompts_fingerprint() == fp


# --- tier2 circuit breaker -------------------------------------------------

FAKE_PRICE = "$9.99 FAKE"


def _add_diamond(repo: FakeRepo, graph_id: int, base: int) -> None:
    """One flagship diamond blaming scraper-agent, with graph-unique run ids."""
    repo.add_bundle(
        make_bundle(
            [
                make_run(
                    base + 1, "orchestrator", graph_id=graph_id,
                    input_inline="scrape 3 products", end_time=1.0,
                ),
                make_run(
                    base + 2, "scraper-agent", graph_id=graph_id,
                    output_inline=f'{{"price": "{FAKE_PRICE}"}}', end_time=2.0,
                ),
                make_run(
                    base + 3, "compliance-agent", graph_id=graph_id,
                    output_inline=f"compliance approved price {FAKE_PRICE}",
                    end_time=3.0,
                ),
                make_run(
                    base + 4, "publisher-agent", graph_id=graph_id,
                    output_inline=f"published listing at {FAKE_PRICE}", end_time=4.0,
                ),
            ],
            [(base + 1, base + 2), (base + 2, base + 3), (base + 3, base + 4)],
            graph_id=graph_id,
        )
    )
    repo.tier1[uid(graph_id)] = Tier1Verdict(
        graph_id=uid(graph_id),
        terminal_judge_verdict="bad",
        terminal_judge_score=0.2,
        terminal_judge_reasoning="final price looks fabricated",
        flags=[],
        flagged=True,
        sampled=False,
    )


def _breaker_judge() -> FakeJudge:
    return FakeJudge(
        node_verdicts={
            "orchestrator": {"task_score": 1.0, "input_flawed": False, "reasoning": "ok"},
            "scraper-agent": {"task_score": 0.1, "input_flawed": False, "reasoning": "fabricated"},
            "compliance-agent": {"task_score": 0.95, "input_flawed": True, "reasoning": "bad input"},
            "publisher-agent": {"task_score": 0.95, "input_flawed": True, "reasoning": "bad input"},
        },
        claims=[FAKE_PRICE],
    )


def test_breaker_trips_on_third_open_incident_for_same_culprit_agent():
    repo = FakeRepo()
    for g in (1, 2, 3):
        _add_diamond(repo, g, base=g * 10)
    streams = FakeStreams()
    processor = Tier2Processor(
        repo, FakeObjectStore(), streams, _breaker_judge(), make_settings()
    )

    for g in (1, 2):
        asyncio.run(
            processor.process(
                Tier2Message(graph_id=str(uid(g)), trigger="tier1", dedup_key=str(uid(g)))
            )
        )
    # Two open incidents: below the default threshold (3) — no breaker yet.
    assert repo.breakers == {}
    assert streams.messages(STREAM_CONTROL_SIGNALS) == []

    asyncio.run(
        processor.process(
            Tier2Message(graph_id=str(uid(3)), trigger="tier1", dedup_key=str(uid(3)))
        )
    )

    # Third open incident blaming scraper-agent: the breaker decision is
    # RECORDED (open) — nothing was stopped; enforcement is SDK-opt-in.
    entry = repo.breakers[("agent_name", "scraper-agent")]
    assert entry["state"] == "open"
    assert "3 open incidents" in entry["reason"]
    assert "threshold 3" in entry["reason"]
    assert "degraded_quality" in entry["reason"]
    assert entry["opened_at"] is not None

    messages = streams.messages(STREAM_CONTROL_SIGNALS)
    assert len(messages) == 1
    assert messages[0] == {
        "schema_version": 1,
        "scope_kind": "agent_name",
        "scope_value": "scraper-agent",
        "state": "open",
        "reason": entry["reason"],
    }
    # Only the culprit's scope tripped; the healthy agents did not.
    assert set(repo.breakers) == {("agent_name", "scraper-agent")}


def test_breaker_does_not_reevaluate_on_non_new_incident():
    repo = FakeRepo()
    for g in (1, 2, 3):
        _add_diamond(repo, g, base=g * 10)
    streams = FakeStreams()
    processor = Tier2Processor(
        repo, FakeObjectStore(), streams, _breaker_judge(), make_settings()
    )
    for g in (1, 2, 3):
        asyncio.run(
            processor.process(
                Tier2Message(graph_id=str(uid(g)), trigger="tier1", dedup_key=str(uid(g)))
            )
        )
    assert len(streams.messages(STREAM_CONTROL_SIGNALS)) == 1

    # Reprocessing graph 3 under a fresh dedup_key updates the same incident
    # (is_new=False) — no second control signal for the same standing state.
    asyncio.run(
        processor.process(
            Tier2Message(
                graph_id=str(uid(3)), trigger="manual", dedup_key="manual-rerun"
            )
        )
    )
    assert len(streams.messages(STREAM_CONTROL_SIGNALS)) == 1
