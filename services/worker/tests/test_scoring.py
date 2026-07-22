"""Per-node scoring: renormalization floor, truncation, schema, heuristics."""

import asyncio

from worker.scoring import (
    composite_score,
    evaluate_heuristics,
    evaluate_schema,
    load_prompt,
    score_node,
    truncate_for_judge,
)
from worker.types import AgentStat, OutputContract

from conftest import FakeJudge, make_run

WEIGHTS = {"schema": 0.35, "judge": 0.40, "heuristics": 0.15}


def _score(run, output, judge, contracts=None, baseline=None, min_weight=0.5):
    return asyncio.run(
        score_node(
            run,
            "the input",
            output,
            contracts or [],
            baseline,
            judge,
            asyncio.Semaphore(2),
            WEIGHTS,
            min_weight,
            load_prompt("judge.md"),
        )
    )


def test_composite_renormalizes_when_judge_none_but_weight_at_floor():
    # schema + heuristics = 0.35 + 0.15 = 0.50 == floor -> not below -> computed.
    score, reason = composite_score(
        {"schema": 1.0, "judge": None, "heuristics": 0.0}, WEIGHTS, 0.5
    )
    assert reason is None
    assert score == 0.7  # (0.35*1 + 0.15*0) / 0.50


def test_composite_unscored_when_remaining_weight_below_floor():
    # Only heuristics present (0.15) with judge None -> below floor -> None.
    score, reason = composite_score(
        {"schema": None, "judge": None, "heuristics": 0.2}, WEIGHTS, 0.5
    )
    assert score is None
    assert reason == "insufficient_components"


def test_score_node_computed_when_judge_fails_but_schema_and_heuristics_present():
    # Judge always fails -> judge component None, but a passing schema contract
    # plus heuristics keep the node scored (weight 0.50 == floor).
    contract = OutputContract(
        agent_name="scraper",
        agent_version_pattern=None,
        json_schema={"type": "object", "required": ["price"]},
    )
    run = make_run(1, "scraper", output_inline='{"price": 5}')
    result = _score(run, '{"price": 5}', FakeJudge(fail=True), contracts=[contract])
    assert result.components["judge"] is None
    assert result.components["schema"] == 1.0
    assert result.score is not None
    assert result.unscored_reason is None


def test_score_node_unscored_when_judge_fails_and_no_contract():
    # No contract -> schema None; judge fails -> only heuristics (0.15) < floor.
    run = make_run(1, "scraper")
    result = _score(run, "a well formed output", FakeJudge(fail=True))
    assert result.score is None
    assert result.unscored_reason == "insufficient_components"


def test_score_node_payload_missing():
    run = make_run(1, "scraper", output_inline=None)
    result = _score(run, None, FakeJudge())
    assert result.score is None
    assert result.unscored_reason == "payload_missing"


def test_score_node_uses_judge_verdict_and_input_flawed():
    run = make_run(1, "compliance")
    judge = FakeJudge(
        node_verdicts={
            "compliance": {"task_score": 0.9, "input_flawed": True, "reasoning": "bad input"}
        }
    )
    result = _score(run, "output", judge)
    assert result.components["judge"] == 0.9
    assert result.input_flawed is True
    assert result.judge_note == "bad input"


def test_truncation_is_deterministic_head_tail_marker():
    text = "H" * (12 * 1024) + "M" * (10 * 1024) + "T" * (4 * 1024)
    out = truncate_for_judge(text)
    assert out == truncate_for_judge(text)
    assert out.startswith("H" * 100)
    assert out.endswith("T" * 100)
    assert "...[truncated 10240 bytes]..." in out


def test_truncation_passthrough_when_small():
    text = "short output"
    assert truncate_for_judge(text) == text


def test_schema_validation_pass_and_fail():
    contract = OutputContract(
        agent_name="scraper",
        agent_version_pattern="1.*",
        json_schema={
            "type": "object",
            "required": ["items"],
            "properties": {"items": {"type": "array"}},
        },
    )
    ok = evaluate_schema('{"items": [1, 2]}', [contract], "scraper", "1.4")
    bad = evaluate_schema('{"nope": true}', [contract], "scraper", "1.4")
    none = evaluate_schema('{"items": []}', [contract], "other", "1.4")
    version_miss = evaluate_schema('{"items": []}', [contract], "scraper", "2.0")
    assert ok == 1.0
    assert bad == 0.0
    assert none is None  # no contract for this agent
    assert version_miss is None  # version pattern does not match


def test_heuristics_penalizes_empty_and_failed_and_repetition():
    run_ok = make_run(1, "a", status="ok")
    run_failed = make_run(2, "a", status="failed")
    assert evaluate_heuristics("", run_ok, None, error_span_ids=[], retry_count=0) == 0.0
    healthy = evaluate_heuristics(
        "a clean and varied sentence output", run_ok, None, error_span_ids=[], retry_count=0
    )
    failed = evaluate_heuristics(
        "a clean and varied sentence output",
        run_failed,
        None,
        error_span_ids=["e1"],
        retry_count=0,
    )
    assert healthy == 1.0
    assert failed < healthy


def test_heuristics_token_zscore_outlier():
    baseline = AgentStat(
        tokens_out_mean=100.0,
        tokens_out_std=10.0,
        iterations_mean=None,
        iterations_std=None,
        sample_count=10,
    )
    run = make_run(1, "a", tokens_out=1000)
    outlier = evaluate_heuristics(
        "varied clean output text here", run, baseline, error_span_ids=[], retry_count=0
    )
    assert outlier < 1.0


def test_contract_violation_forces_cut_point_even_with_high_judge():
    """The wedge fixture: think silently rewrites file_type docx->md.

    A fluent judge may pass the output (task_score 1.0), but the deterministic
    input-contract check must dominate and force the node below any sane blame
    threshold, so blame localises here as a cut_point culprit.
    """
    run = make_run(1, "think")
    input_json = '{"request": "Vytvor nabidku", "file_type": "docx"}'
    output_json = '{"plan": {"file_type": "md", "sections": ["a", "b"]}}'
    result = asyncio.run(
        score_node(
            run,
            input_json,
            output_json,
            [],
            None,
            FakeJudge(),  # returns task_score 1.0 -> proves the override dominates
            asyncio.Semaphore(2),
            WEIGHTS,
            0.5,
            load_prompt("judge.md"),
        )
    )
    assert result.score is not None and result.score <= 0.15
    assert result.components.get("contract") == 0.0
    assert result.unscored_reason is None
    assert result.judge_note and "file_type" in result.judge_note


def test_contract_preserved_is_not_penalised():
    """When the carried-through parameter is unchanged, no contract penalty."""
    run = make_run(2, "act")
    result = asyncio.run(
        score_node(
            run,
            '{"file_type": "md", "note": "x"}',
            '{"plan": {"file_type": "md"}}',
            [],
            None,
            FakeJudge(),
            asyncio.Semaphore(2),
            WEIGHTS,
            0.5,
            load_prompt("judge.md"),
        )
    )
    assert "contract" not in result.components
    assert result.score is not None and result.score > 0.5
