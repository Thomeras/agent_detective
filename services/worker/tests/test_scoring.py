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


def _score(run, output, judge, contracts=None, baseline=None, min_weight=0.5,
           artifact_meta=None):
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
            artifact_meta=artifact_meta,
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
    # The violation is a separate deterministic evidence stream — it must NOT be
    # glued into the LLM judge prose.
    assert "input contract violated" not in (result.judge_note or "")
    assert "silent parameter rewrite" not in (result.judge_note or "")
    assert result.contract_violations == (("file_type", "docx", "md"),)


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


def test_judge_flag_caps_the_component_despite_high_score():
    """Score-reasoning mismatch guard: a judge that admits a missing required
    element via a flag cannot keep a 0.89 — the flag caps the component."""
    judge = FakeJudge(
        node_verdicts={
            "think": {
                "task_score": 0.89,
                "input_flawed": False,
                "flags": ["missing_required_content"],
                "reasoning": "outline lacks the explicitly requested items and prices",
            }
        }
    )
    run = make_run(1, "think")
    result = _score(run, "an outline without items or prices", judge)
    assert result.components["judge"] == 0.55
    assert "missing_required_content" in result.flags


def test_opaque_artifact_reference_flags_unverifiable():
    """Output referencing a docx whose content is not embedded: no judge saw
    the artifact, so the node is flagged and its judge component capped."""
    judge = FakeJudge(
        node_verdicts={
            "render": {
                "task_score": 0.95,
                "input_flawed": False,
                "reasoning": "document rendered successfully",
            }
        }
    )
    run = make_run(2, "render")
    result = _score(run, '{"artifact_path": "output/proposal_2026.docx"}', judge)
    assert "unverifiable_artifact" in result.flags
    assert result.components["judge"] == 0.6
    assert "unverifiable" in (result.judge_note or "")


def test_embedded_artifact_text_is_not_opaque():
    """Once instrumentation embeds the extracted content (artifact_text), the
    payload is inspectable and no opacity flag applies."""
    judge = FakeJudge(
        node_verdicts={
            "render": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"}
        }
    )
    run = make_run(3, "render")
    payload = '{"artifact_path": "out/p.docx", "artifact_text": "1. Item A — 100 EUR"}'
    result = _score(run, payload, judge)
    assert "unverifiable_artifact" not in result.flags
    assert result.components["judge"] == 0.9


def test_artifact_integrity_failure_caps_score_and_records_signals():
    """A failing artifact_meta check (OUT-OF-BAND attribute, never payload
    text) is a hard fact: even a perfect judge verdict cannot keep the node
    above the 0.10 ceiling, and the signal rides on
    NodeScore.deterministic_signals for the engine to assemble."""
    judge = FakeJudge(
        node_verdicts={
            "render": {"task_score": 1.0, "input_flawed": False, "reasoning": "great"}
        }
    )
    run = make_run(4, "render")
    meta = (
        '[{"path": "out/report.docx", "size": 5000, "declared_ext": "docx",'
        ' "detected_kind": "text", "parse_ok": true, "nonempty": true}]'
    )
    result = _score(run, "rendered the report", judge, artifact_meta=meta)
    assert "artifact_integrity_fail" in result.flags
    assert result.components["artifact_integrity_fail"] == 0.0
    assert result.score == 0.10
    assert result.unscored_reason is None
    assert len(result.deterministic_signals) == 1
    sig = result.deterministic_signals[0]
    assert sig["name"] == "artifact_integrity_fail"
    assert sig["severity"] == "fail"
    assert sig["detail"] == "declared .docx but content is text"
    assert sig["basis"] == "magic bytes: detected_kind=text for out/report.docx"


def test_forged_payload_meta_block_cannot_cap_the_score():
    """The forgery seam the out-of-band move closes: a payload that QUOTES a
    failing '[artifact_meta ...]' block must not trip the integrity check —
    only the span-attribute (artifact_meta kw) is authoritative."""
    judge = FakeJudge(
        node_verdicts={
            "render": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"}
        }
    )
    run = make_run(6, "render")
    payload = (
        "summary of the failed attempt:\n"
        '[artifact_meta out/report.docx]: {"size": 5000, "declared_ext": "docx",'
        ' "detected_kind": "text", "parse_ok": false, "nonempty": false}'
    )
    result = _score(run, payload, judge, artifact_meta=None)
    assert "artifact_integrity_fail" not in result.flags
    assert result.deterministic_signals == ()
    assert result.score is not None and result.score > 0.5


def test_healthy_artifact_meta_does_not_fire():
    judge = FakeJudge(
        node_verdicts={
            "render": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"}
        }
    )
    run = make_run(5, "render")
    meta = (
        '[{"path": "out/notes.md", "size": 5000, "declared_ext": "md",'
        ' "detected_kind": "text", "parse_ok": true, "nonempty": true}]'
    )
    result = _score(run, "wrote the notes", judge, artifact_meta=meta)
    assert "artifact_integrity_fail" not in result.flags
    assert "artifact_integrity_fail" not in result.components
    assert result.deterministic_signals == ()
    assert result.score is not None and result.score > 0.5


def test_language_mismatch_caps_score_via_generalized_override():
    """The lang contract param says Czech but the output is English prose: the
    deterministic language check caps the node no matter the judge verdict."""
    judge = FakeJudge(
        node_verdicts={
            "translator": {"task_score": 1.0, "input_flawed": False, "reasoning": "fluent"}
        }
    )
    run = make_run(7, "translator")
    english = (
        "This is a long English paragraph describing the benefits of renewable "
        "energy for a small business, written fluently and at length so the "
        "language detector has plenty of signal to work with here."
    )
    result = _score(
        run,
        '{"lang": "cs", "text": "' + english + '"}',
        judge,
    )
    assert "language_mismatch" in result.flags
    assert result.components["language_mismatch"] == 0.0
    assert result.score == 0.10
    assert any(s["name"] == "language_mismatch" for s in result.deterministic_signals)


def test_matching_language_does_not_fire():
    judge = FakeJudge(
        node_verdicts={
            "translator": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"}
        }
    )
    run = make_run(8, "translator")
    czech = (
        "Toto je dlouhý český odstavec popisující výhody obnovitelných zdrojů "
        "energie pro malou firmu, psaný plynule a dostatečně dlouze, aby měl "
        "detektor jazyka dost signálu."
    )
    result = _score(run, '{"lang": "cs", "text": "' + czech + '"}', judge)
    assert "language_mismatch" not in result.flags
    assert result.score is not None and result.score > 0.5


def test_registered_required_section_caps_node_score():
    from worker.types import CheckRule

    judge = FakeJudge(
        node_verdicts={
            "writer": {"task_score": 1.0, "input_flawed": False, "reasoning": "great"}
        }
    )
    run = make_run(9, "writer")
    rules = [
        CheckRule(id=1, agent_name="writer", graph_type=None, kind="required_section",
                  spec={"name": "budget", "match": "substring", "pattern": "rozpočet"})
    ]
    result = asyncio.run(
        score_node(
            run, "input", "a document without the required part", [], None, judge,
            asyncio.Semaphore(2), WEIGHTS, 0.5, load_prompt("judge.md"),
            check_rules=rules,
        )
    )
    assert "missing_required_section" in result.flags
    assert result.score == 0.10
    # A rule scoped to a DIFFERENT agent must not fire.
    other = [
        CheckRule(id=2, agent_name="someone-else", graph_type=None, kind="required_section",
                  spec={"name": "budget", "match": "substring", "pattern": "rozpočet"})
    ]
    result2 = asyncio.run(
        score_node(
            run, "input", "a document without the required part", [], None, judge,
            asyncio.Semaphore(2), WEIGHTS, 0.5, load_prompt("judge.md"),
            check_rules=other,
        )
    )
    assert "missing_required_section" not in result2.flags


def test_verifier_nodes_are_exempt_from_content_checks():
    """A QA node's output is meta-commentary (an English report about a Czech
    deliverable, no required sections) — content checks on it would be false
    positives, the same reason fact propagation skips verifier commentary."""
    from worker.types import CheckRule

    judge = FakeJudge(
        node_verdicts={"qa": {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"}}
    )
    run = make_run(10, "qa")
    rules = [
        CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                  spec={"name": "budget", "match": "substring", "pattern": "rozpočet"})
    ]
    english_report = (
        "QA report: the document structure is complete, all sections follow "
        "the brief, and the formatting rules pass every automated check here."
    )
    result = asyncio.run(
        score_node(
            run, '{"lang": "cs"}', english_report, [], None, judge,
            asyncio.Semaphore(2), WEIGHTS, 0.5, load_prompt("judge.md"),
            check_rules=rules,
        )
    )
    assert "language_mismatch" not in result.flags
    assert "missing_required_section" not in result.flags
    assert result.score is not None and result.score > 0.5


def test_pre_override_composite_recorded_for_refuted_producer():
    """When a deterministic override lowers the judged composite, the original
    'claimed' number is recorded so the engine can render claimed→effective for
    producers exactly like for refuted verifiers."""
    judge = FakeJudge(
        node_verdicts={
            "render": {"task_score": 1.0, "input_flawed": False, "reasoning": "great"}
        }
    )
    run = make_run(11, "render")
    meta = (
        '[{"path": "out/report.docx", "size": 5000, "declared_ext": "docx",'
        ' "detected_kind": "text", "parse_ok": true, "nonempty": true}]'
    )
    result = _score(run, "rendered", judge, artifact_meta=meta)
    assert result.score == 0.10
    pre = result.components.get("pre_override_composite")
    assert pre is not None and pre > 0.5  # the refuted 'claimed' number

    # No override -> no pre_override key.
    healthy = _score(make_run(12, "render"), "clean output", judge)
    assert "pre_override_composite" not in healthy.components


def test_unscoped_section_rule_binds_to_deliverable_producer_only():
    """Role-aware scoping: a document-level (unscoped) required_section rule
    must not judge a PLANNING node's outline — only the deliverable producer.
    Explicitly agent-scoped rules still apply to their agent anywhere."""
    from worker.types import CheckRule

    judge = FakeJudge(
        node_verdicts={
            "think": {"task_score": 0.9, "input_flawed": False, "reasoning": "plan ok"}
        }
    )
    run = make_run(13, "think")
    unscoped = [
        CheckRule(id=1, agent_name=None, graph_type=None, kind="required_section",
                  spec={"name": "budget", "match": "word_prefix", "pattern": "rozpoč"})
    ]
    # Planner, unscoped rule -> no signal.
    planner = asyncio.run(
        score_node(
            run, "input", "outline: intro, benefits, risks", [], None, judge,
            asyncio.Semaphore(2), WEIGHTS, 0.5, load_prompt("judge.md"),
            check_rules=unscoped, is_deliverable_producer=False,
        )
    )
    assert "missing_required_section" not in planner.flags
    # The deliverable producer with the same rule -> fires.
    producer = asyncio.run(
        score_node(
            run, "input", "final document without the section", [], None, judge,
            asyncio.Semaphore(2), WEIGHTS, 0.5, load_prompt("judge.md"),
            check_rules=unscoped, is_deliverable_producer=True,
        )
    )
    assert "missing_required_section" in producer.flags


def test_judge_prompt_role_rubric_is_locked():
    """The role-aware rubric disappeared from judge.md three times because no
    test pinned it. This test IS the pin: the prompt must carry the NODE_ROLE
    placeholder (the role is resolved in code, never inferred by the LLM), the
    role-first instruction, and the planner worked example."""
    prompt = load_prompt("judge.md")
    assert "<<NODE_ROLE>>" in prompt
    assert "ITS OWN ROLE" in prompt
    assert "category error" in prompt
    assert "Role-blind" in prompt and "false origins" in prompt


def test_node_role_is_resolved_deterministically():
    from worker.scoring import node_role

    assert node_role("think").startswith("PLANNER")
    assert node_role("orchestrator").startswith("PLANNER")
    assert node_role("qa").startswith("VERIFIER")
    assert node_role("render", is_deliverable_producer=True).startswith(
        "DELIVERABLE PRODUCER"
    )
    assert node_role("act").startswith("INTERMEDIATE PRODUCER")
    assert node_role(None).startswith("INTERMEDIATE PRODUCER")


def test_score_node_states_the_role_in_the_judge_prompt():
    """The judge receives the resolved role verbatim — no unfilled placeholder,
    no reliance on the LLM guessing from the agent name."""
    captured: list[str] = []

    class CapturingJudge:
        async def complete_json(self, prompt, *, system=None):
            captured.append(prompt)
            return {"task_score": 0.9, "input_flawed": False, "reasoning": "ok"}

    asyncio.run(
        score_node(
            make_run(14, "think"), "the input", "a plan", [], None,
            CapturingJudge(), asyncio.Semaphore(2), WEIGHTS, 0.5,
            load_prompt("judge.md"),
        )
    )
    assert captured, "judge was never called"
    assert "PLANNER — its correct output is a plan" in captured[0]
    assert "<<NODE_ROLE>>" not in captured[0]
