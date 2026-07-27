"""Per-node scoring: renormalization floor, truncation, schema, heuristics."""

import asyncio

from worker.scoring import (
    ARTIFACT_OPAQUE,
    ARTIFACT_PARTIAL,
    _body_prose_word_count,
    classify_artifact_visibility,
    composite_score,
    contract_violations,
    evaluate_heuristics,
    evaluate_schema,
    load_prompt,
    score_node,
    truncate_for_judge,
)
from worker.types import FLAG_UNINSPECTED_MEDIA, AgentStat, OutputContract

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


def test_score_node_empty_payload_is_unscored_not_zero():
    """An empty output.value must not be scored 0.0.

    Orchestrator/wrapper spans (LangGraph roots, CrewAI kickoff, the archintel
    `intel` root) legitimately carry no output of their own. Scoring "" as a
    hard 0.0 invented the strongest possible verdict — "demonstrably bad" —
    from no evidence, and made those wrappers the culprit of every run they
    appeared in. Unknown is the only honest answer.
    """
    run = make_run(1, "orchestrator")
    result = _score(run, "", FakeJudge())
    assert result.score is None
    assert result.unscored_reason == "payload_missing"
    assert result.components == {"schema": None, "judge": None, "heuristics": None}


def test_score_node_whitespace_only_payload_is_unscored():
    # Same rule for a payload that only looks present.
    result = _score(make_run(1, "orchestrator"), "  \n\t ", FakeJudge())
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


def test_contract_violation_travels_as_evidence_not_a_floored_score():
    """The wedge fixture: think silently rewrites file_type docx->md.

    CHANNEL DECOUPLING (R4): a fluent judge passes the output (task_score 1.0) and
    that judged score is kept UNTOUCHED — the contract check no longer floors it to
    a localisation sentinel. The violation travels as its OWN evidence stream
    (contract_violations); the blame engine localises on it via the deterministic
    channel (blame_engine/tests/test_channel_via.py::test_deterministic_candidacy).
    The old `score <= 0.15` assertion encoded exactly the sentinel this removed.
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
    # Judged score is NOT floored — the judge's verdict survives above the blame
    # threshold as the measured quality; localisation is the engine's job now.
    assert result.score is not None and result.score > 0.5
    assert result.unscored_reason is None
    # The violation is a separate deterministic evidence stream — it must NOT be
    # glued into the LLM judge prose.
    assert "input contract violated" not in (result.judge_note or "")
    assert "silent parameter rewrite" not in (result.judge_note or "")
    assert result.contract_violations == (("file_type", "docx", "md"),)


def test_fan_in_with_conflicting_branch_values_reports_no_violation():
    """A joiner's input carries one value per incoming branch. The collector kept
    whichever scalar the walk reached FIRST, so a merge of `lang=cs` and
    `lang=en` into a `cs` output was reported as the joiner rewriting en->cs — a
    named violation, with the joiner as its named origin, on a contract it was
    never given. Two conflicting inputs mean the contract is unknown here, and
    unknown must stay unknown."""
    merged = (
        '{"branches": [{"lang": "en", "text": "a"}, {"lang": "cs", "text": "b"}]}'
    )
    assert contract_violations(merged, '{"lang": "cs", "text": "ab"}') == []


def test_fan_in_with_agreeing_branch_values_still_catches_the_rewrite():
    """The ambiguity rule must not become a blanket amnesty for joiners: when
    every branch carried the SAME value, the contract is unambiguous and a
    rewrite downstream of the merge is still a violation."""
    merged = (
        '{"branches": [{"lang": "cs", "text": "a"}, {"lang": "cs", "text": "b"}]}'
    )
    assert contract_violations(merged, '{"lang": "en", "text": "ab"}') == [
        ("lang", "cs", "en")
    ]


def test_declared_contract_params_stand_in_for_prose_input():
    """Convention lane (migration 0011): a foreign pipeline whose input payload
    is prose declares the carried params out-of-band — the rewrite observed in
    a JSON output is still a deterministic violation."""
    assert contract_violations(
        "Write the quarterly report as requested.",
        '{"file_type": "md", "content": "..."}',
        declared='{"file_type": "pdf"}',
    ) == [("file_type", "pdf", "md")]


def test_declared_contract_params_need_an_observable_output_value():
    """Prose/code output shows no value for the declared key: no diff, no
    violation — honest unverified, never a false alarm."""
    assert (
        contract_violations(
            "prose input", "import pygame  # just code", declared='{"file_type": "pdf"}'
        )
        == []
    )


def test_declared_contract_params_extend_the_key_search():
    """A declared key OUTSIDE the built-in contract-key list is still checked —
    the declaration says what is carried (the CrewAI spec-inversion class:
    producer flips a rule the pipeline never spells as file_type/lang)."""
    assert contract_violations(
        None, '{"wall_rule": "wrap-around"}', declared='{"wall_rule": "game-over"}'
    ) == [("wall_rule", "game-over", "wrap-around")]


def test_declared_contract_params_override_the_payload_parse():
    """The attribute is stamped by instrumentation; payload text is forgeable
    by content — on conflict the out-of-band declaration is the contract."""
    assert contract_violations(
        '{"file_type": "docx"}', '{"file_type": "md"}', declared='{"file_type": "pdf"}'
    ) == [("file_type", "pdf", "md")]


def test_malformed_declared_contract_params_are_ignored():
    assert (
        contract_violations(
            '{"file_type": "md"}', '{"file_type": "md"}', declared="not json"
        )
        == []
    )
    assert (
        contract_violations(None, '{"file_type": "md"}', declared='["not", "an object"]')
        == []
    )


def test_score_node_reads_declared_params_from_the_run():
    from dataclasses import replace

    run = replace(make_run(3, "coder"), contract_params='{"file_type": "pdf"}')
    result = asyncio.run(
        score_node(
            run,
            "please produce the report",
            '{"file_type": "md"}',
            [],
            None,
            FakeJudge(),
            asyncio.Semaphore(2),
            WEIGHTS,
            0.5,
            load_prompt("judge.md"),
        )
    )
    assert result.contract_violations == (("file_type", "pdf", "md"),)


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


def _illustrated_dossier() -> str:
    """The SHAPE from two production runs, at test scale: a markdown dossier
    whose whole text is in the payload, illustrating itself with
    `![alt](photos/x.jpg)`.

    Scale honesty: this fixture is 1,163 bytes and 147 body-prose words (both
    pinned by test_illustrated_dossier_fixture_measurements below), not the
    multi-kilobyte payload the production incident carried. What it reproduces
    is the structure the classifier keys on — embedded images inside a body that
    stands on its own — and nothing in this suite exercises payload SIZE."""
    return """# Dossier: Villa Kalina, Cadastral Area Bubenec

## Overview
The property occupies a plot of 1,240 square metres with a southern exposure and
direct access from the eastern service road. The main building was completed in
1932 and last renovated in 2019, when the roof structure and all window units
were replaced. Ownership has been unbroken since 1996 and no liens are recorded.

![Front elevation from the street](photos/afea18287da0626c.jpg)

## Structural condition
The roof covering is ceramic tile laid in 2019; no displaced tiles or moss
accumulation were observed from ground level. The facade retains its original
lime render, with hairline cracking along the northern wall close to grade,
consistent with settlement rather than active movement. Rainwater goods are
copper and appear original, with visible patina but no perforation.

![North wall detail showing hairline cracking](photos/8c5300c9b9545da4.jpg)

## Valuation
Comparable transactions recorded in the district over the trailing eighteen
months support a range of 18.5 to 21.0 million crowns once adjusted for plot
area, the 2019 renovation, and the absence of an off-street parking space.
"""


def test_illustrated_dossier_fixture_measurements():
    """Pins what the fixture actually IS, because the previous round described it
    as "a real ~18 KB markdown dossier" in three docstrings and a code comment
    while it was 1,163 bytes. A word-count rule argued about in prose and never
    measured is how the 60-word bar came to be defended with a number nobody had
    run."""
    text = _illustrated_dossier()
    assert len(text.encode("utf-8")) == 1163
    assert _body_prose_word_count(text) == 147


def test_illustrated_markdown_deliverable_is_graded_on_its_text():
    """The regression that discarded two production verdicts: a markdown dossier
    whose text WAS fully in the payload was declared unverifiable because it
    embedded `![alt](photos/8c5300c9.jpg)`. Images embedded inside a readable
    document are illustrations, not the artifact — no opacity flag, no cap."""
    judge = FakeJudge(
        node_verdicts={
            "dossier": {
                "task_score": 0.92,
                "input_flawed": False,
                "reasoning": "covers every requested section",
            }
        }
    )
    run = make_run(5, "dossier")
    result = _score(run, _illustrated_dossier(), judge)
    assert "unverifiable_artifact" not in result.flags
    assert result.components["judge"] == 0.92


def test_illustrated_deliverable_records_the_images_it_could_not_see():
    """Checkable is not the same as fully verified: the photos may carry meaning
    the prose does not, so the grade is reported as PARTIAL — the note says it
    came from the text alone AND a flag carries that limit as data, because a
    sentence appended to a reasoning string is not something a consumer of the
    score can key off."""
    judge = FakeJudge(
        node_verdicts={
            "dossier": {"task_score": 0.92, "input_flawed": False, "reasoning": "good"}
        }
    )
    run = make_run(6, "dossier")
    result = _score(run, _illustrated_dossier(), judge)
    note = result.judge_note or ""
    assert "text only" in note
    assert "photos/8c5300c9b9545da4.jpg" in note
    assert FLAG_UNINSPECTED_MEDIA in result.flags
    # PARTIAL is a limit, not a penalty: inventing a deduction out of "we did
    # not look" is the mirror of inventing a pass.
    assert result.components["judge"] == 0.92


def test_payload_that_is_only_an_image_reference_is_still_opaque():
    """The protection survives on the media axis too: when the deliverable IS
    the picture, the payload is an announcement and there is nothing to grade."""
    judge = FakeJudge(
        node_verdicts={
            "chart": {
                "task_score": 0.95,
                "input_flawed": False,
                "reasoning": "chart produced as requested",
            }
        }
    )
    run = make_run(7, "chart")
    result = _score(run, "Done. The revenue chart is at charts/q3_revenue.png", judge)
    assert "unverifiable_artifact" in result.flags
    assert result.components["judge"] == 0.6


_VERBOSE_IMAGE_ANNOUNCEMENT = (
    "I have finished the logo. It uses a deep indigo as the primary colour with a "
    "warm sand accent, chosen so the mark reads well at small sizes and keeps enough "
    "contrast in the reversed variant. The mark itself is a stylised compass rose "
    "whose needle forms the letter A. I exported it at 512 pixels and at 2048 pixels "
    "so it can be used both in the app header and on printed material. The palette is "
    "documented in the file header for anyone who needs to rebuild it later. "
    "The file is saved to assets/logo.png."
)


def test_verbose_announcement_about_an_image_is_not_gradeable_prose():
    """The false CERTAINTY a bare word-count bar bought: this payload is 87 body
    words — comfortably over the 60-word bar — and every one of them is a claim
    ABOUT a picture nobody opened. Under the bar alone it came out flagless with
    the judge's 0.95 intact. Volume of prose is not evidence that a deliverable
    is present; containing the image is."""
    assert _body_prose_word_count(_VERBOSE_IMAGE_ANNOUNCEMENT) == 87
    judge = FakeJudge(
        node_verdicts={
            "designer": {
                "task_score": 0.95,
                "input_flawed": False,
                "reasoning": "the described palette is coherent",
            }
        }
    )
    run = make_run(10, "designer")
    result = _score(run, _VERBOSE_IMAGE_ANNOUNCEMENT, judge)
    assert "unverifiable_artifact" in result.flags
    assert result.components["judge"] == 0.6


def test_markdown_gallery_is_not_gradeable_on_its_captions():
    """The real shape of a photo gallery, not a synthetic one: twelve
    `![alt](path)` lines with genuine alt text. The captions used to count as
    body prose (72 words, all caption, zero body), so the gallery cleared the
    bar and was graded on descriptions of pictures nobody opened. Alt text is a
    claim about an artifact, exactly like prose about a .docx, and can no more
    stand in for the artifact."""
    gallery = "\n\n".join(
        f"![Front elevation seen from the street](photos/img_{i:03d}.jpg)"
        for i in range(12)
    )
    assert _body_prose_word_count(gallery) == 0
    judge = FakeJudge(node_verdicts={"gallery": {"task_score": 0.9, "reasoning": "ok"}})
    result = _score(make_run(11, "gallery"), gallery, judge)
    assert "unverifiable_artifact" in result.flags
    assert result.components["judge"] == 0.6


def test_photo_manifest_cannot_pass_itself_off_as_deliverable_text():
    """Length is measured in PROSE, references struck out first — otherwise a
    listing of sixty image paths is kilobytes of 'text' and self-exempts."""
    judge = FakeJudge(node_verdicts={"gallery": {"task_score": 0.9, "reasoning": "ok"}})
    run = make_run(8, "gallery")
    manifest = "photos: " + ", ".join(f"photos/img_{i:03d}.jpg" for i in range(60))
    result = _score(run, manifest, judge)
    assert "unverifiable_artifact" in result.flags


def _embedded_body(words: int) -> str:
    """`words` body-prose words next to one embedded image."""
    return " ".join(["prose"] * words) + "\n\n![a caption here](photos/x.jpg)\n"


def test_body_prose_bar_boundary_is_pinned():
    """The 60-word bar was a strict `<` with nothing fixing its edge, so a later
    refactor could move it by one and silently flip which payloads get a verdict
    withheld. 59 body words is an announcement, 60 is a document — pinned on both
    sides so the number cannot drift unnoticed."""
    assert _body_prose_word_count(_embedded_body(59)) == 59
    assert _body_prose_word_count(_embedded_body(60)) == 60
    assert classify_artifact_visibility(_embedded_body(59)).state == ARTIFACT_OPAQUE
    assert classify_artifact_visibility(_embedded_body(60)).state == ARTIFACT_PARTIAL


def test_html_img_embed_is_recognised_like_the_markdown_one():
    """The discriminator is "the payload contains the image", not "the payload is
    markdown". An HTML deliverable embeds with `<img src=…>` and must not be
    punished for the format."""
    html = "<h1>Site audit</h1><p>" + " ".join(["finding"] * 70) + "</p>"
    html += '<img src="shots/home.png" alt="home page above the fold">'
    visibility = classify_artifact_visibility(html)
    assert visibility.state == ARTIFACT_PARTIAL
    assert visibility.uninspected_refs == ("shots/home.png",)


def test_embedded_artifact_text_does_not_verify_the_figures_beside_it():
    """Extraction produces text, never pixels. A payload carrying a document's
    extracted body AND pointing at its figures used to short-circuit to fully
    verified on the artifact_text marker alone, silently claiming the figures
    checked out. It is graded on the text and says so."""
    payload = (
        '{"artifact_path": "out/report.docx", '
        '"artifact_text": "Q3 revenue rose 12 percent against plan.", '
        '"figures": ["charts/q3.png"]}'
    )
    visibility = classify_artifact_visibility(payload)
    assert visibility.state == ARTIFACT_PARTIAL
    assert visibility.uninspected_refs == ("charts/q3.png",)
    judge = FakeJudge(node_verdicts={"render": {"task_score": 0.9, "reasoning": "ok"}})
    result = _score(make_run(13, "render"), payload, judge)
    assert "unverifiable_artifact" not in result.flags
    assert FLAG_UNINSPECTED_MEDIA in result.flags
    assert result.components["judge"] == 0.9


def test_merely_mentioning_an_image_earns_no_uninspected_caveat():
    """The caveat is for a payload we READ that contains pictures, and it used to
    land on any node whose text happened to name an image file — a planner
    saying "then render charts/q3.png" got "verified on the payload's text only"
    stapled to its judge note about work it had not done. Naming a file is not
    illustrating a document; that payload is opaque, and the caveat belongs to
    the partial case only."""
    judge = FakeJudge(
        node_verdicts={"plan": {"task_score": 0.8, "reasoning": "sound plan"}}
    )
    plan = (
        "Step one, pull the ledger extract. Step two, reconcile it against the "
        "bank statement. Step three, render the variance chart to charts/q3.png "
        "and hand it to the writer for the commentary section."
    )
    result = _score(make_run(12, "plan"), plan, judge)
    assert FLAG_UNINSPECTED_MEDIA not in result.flags
    assert "text only" not in (result.judge_note or "")


def test_long_prose_about_a_docx_is_still_opaque():
    """Asymmetry, deliberately: prose is exactly what a SUMMARY of a document
    looks like, so it can never prove the document is present. Only the media
    axis lets payload text stand in — no amount of prose is ever the image."""
    judge = FakeJudge(
        node_verdicts={"render": {"task_score": 0.95, "reasoning": "thorough"}}
    )
    run = make_run(9, "render")
    payload = (
        "I produced the client proposal as requested. "
        + "It covers scope, timeline, pricing, staffing and the acceptance "
        "criteria agreed in the kickoff call, section by section. " * 8
        + "The file is saved at out/proposal.docx."
    )
    result = _score(run, payload, judge)
    assert "unverifiable_artifact" in result.flags
    assert result.components["judge"] == 0.6


def test_artifact_integrity_failure_records_signal_without_flooring_score():
    """A failing artifact_meta check (OUT-OF-BAND attribute, never payload text)
    is a hard fact. Under channel decoupling (R4) it no longer caps the judged
    score — it rides on NodeScore.deterministic_signals (fail severity) as an
    independent evidence stream the engine localises on. The judged score stays
    untouched above threshold."""
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
    assert result.score is not None and result.score > 0.5  # judged, not floored
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
    assert result.score is not None and result.score > 0.5  # judged, not floored (R4)
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
    assert result.score is not None and result.score > 0.5  # judged, not floored (R4)
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


def test_producers_get_no_claimed_to_effective_override():
    """Regression guard (R4): producers no longer get a claimed→effective override.

    The judged score is never overwritten by a deterministic fault, so there is no
    'claimed' number to strike through and NO `pre_override_composite` component.
    The deterministic evidence rides on deterministic_signals; the engine renders
    'judged X · check FAILED', not a struck-through sentinel. Re-introducing a
    producer floor would resurrect exactly the multiplexing this refactor removed."""
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
    # Judged score untouched, and the fault is present only as evidence.
    assert result.score is not None and result.score > 0.5
    assert "pre_override_composite" not in result.components
    assert any(
        s["name"] == "artifact_integrity_fail" for s in result.deterministic_signals
    )

    # Healthy node: no signals, no override key either.
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


def test_judge_prompt_forbids_reading_the_handoff_as_a_spec():
    """In a pipeline the INPUT is mostly the predecessor's OUTPUT. Reading it as
    a request made the judge penalise every node for not repeating its
    predecessor — real case: `enrich` scored 0.56 for "does not include the
    requested financial data" (its input carried collect's financials) while its
    own empty ownership result went unmentioned. This test pins the rule."""
    # Normalised, so re-wrapping the prompt cannot silently break the pin.
    prompt = " ".join(load_prompt("judge.md").split())
    assert "material, not a checklist" in prompt
    # Requirements have exactly three sources; upstream presence is not one.
    assert "never makes it required downstream" in prompt
    assert "does not repeat its predecessor's" in prompt
    # An honestly empty lookup is not a defect — the product's core principle.
    assert "an empty lookup result is not the same as a failed step" in prompt


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


def test_intermediate_role_does_not_point_the_judge_at_its_input():
    """The old wording — "judge it against what its input asked THIS step to
    produce" — WAS the echo bug: in a chain the input is the previous step's
    output, so the judge demanded it back. The role must frame the handoff as
    material, not as a request."""
    from worker.scoring import node_role

    role = node_role("enrich")
    assert "input asked" not in role
    assert "NOT a checklist" in role
    assert "ITS OWN contribution" in role


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
