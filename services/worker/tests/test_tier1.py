"""Tier 1: deterministic flags, one terminal judge call, tier2 publish/sampling."""

import asyncio

from worker.scoring import _body_prose_word_count
from worker.tier1 import Tier1Processor
from worker.types import (
    FLAG_UNINSPECTED_MEDIA,
    STREAM_GRAPHS_TIER2,
    OutputContract,
)

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


# ---- Terminal rubric split (content vs form) ----------------------------------


def test_split_rubric_form_breach_is_hard_flag_and_persisted():
    """Content ok + form bad (md shipped where PDF was asked): the run must
    page tier2 via the terminal_form_breach HARD flag — before the split a
    form-only miss reached tier2 only via sampling — and the form dimension
    (with the verbatim requirement quote) must persist for reconciliation."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "writer", end_time=2.0)],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={
            "content": {"verdict": "ok", "score": 1.0, "reasoning": "content complete"},
            "form": {
                "verdict": "bad",
                "requirement": "jako PDF",
                "observed": "markdown text",
                "reasoning": "markdown shipped where PDF was requested",
            },
        }
    )
    streams = run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    # Content dimension carries the stored verdict columns.
    assert verdict.terminal_judge_verdict == "ok"
    assert verdict.terminal_judge_score == 1.0
    # Form dimension: HARD flag + persisted split with the verbatim quote.
    assert "terminal_form_breach" in verdict.flags
    assert verdict.flagged is True
    assert verdict.terminal_form == {
        "verdict": "bad",
        "requirement": "jako PDF",
        "observed": "markdown text",
        "reasoning": "markdown shipped where PDF was requested",
    }
    messages = streams.messages(STREAM_GRAPHS_TIER2)
    assert len(messages) == 1
    assert messages[0]["trigger"] == "tier1"


def test_split_rubric_form_ok_does_not_flag():
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "writer", end_time=2.0)],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={
            "content": {"verdict": "ok", "score": 0.95, "reasoning": "good"},
            "form": {"verdict": "ok", "requirement": "jako PDF",
                     "observed": "pdf", "reasoning": "matches"},
        }
    )
    run_tier1(repo, judge=judge, settings=make_settings(tier2_sample_pct=0))
    verdict = _verdict(repo)
    assert "terminal_form_breach" not in verdict.flags
    assert verdict.flagged is False
    assert verdict.terminal_form["verdict"] == "ok"


def test_split_rubric_content_bad_form_bad_are_independent():
    """Both dimensions bad: content carries the verdict, form adds its flag —
    two faults recorded, neither masked by the other."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "writer", end_time=2.0)],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={
            "content": {"verdict": "bad", "score": 0.3, "reasoning": "missing figures"},
            "form": {"verdict": "bad", "requirement": "jako PDF",
                     "observed": "markdown text", "reasoning": "wrong format"},
        }
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "bad"
    assert "terminal_form_breach" in verdict.flags


def test_legacy_flat_terminal_shape_still_parses_with_no_form():
    """Cassette replay / older judge responses use the flat single-verdict
    shape: accepted as content-only, terminal_form stays None."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "writer", end_time=2.0)],
            [(1, 2)],
        )
    )
    judge = FakeJudge(terminal={"verdict": "bad", "score": 0.2, "reasoning": "wrong"})
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "bad"
    assert verdict.terminal_form is None
    assert "terminal_form_breach" not in verdict.flags


def _illustrated_dossier() -> str:
    """The SHAPE from two production runs, at test scale: a markdown dossier
    whose whole text is in the payload, illustrating itself with
    `![alt](photos/x.jpg)`.

    Scale honesty: 1,075 bytes and 134 body-prose words (pinned by
    test_illustrated_dossier_fixture_measurements), not the multi-kilobyte
    payload the production incident carried. It reproduces the structure the
    classifier keys on, not the size."""
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
consistent with settlement rather than active movement.

![North wall detail showing hairline cracking](photos/8c5300c9b9545da4.jpg)

## Valuation
Comparable transactions recorded in the district over the trailing eighteen
months support a range of 18.5 to 21.0 million crowns once adjusted for plot
area, the 2019 renovation, and the absence of an off-street parking space.
"""


def test_illustrated_dossier_fixture_measurements():
    """Pins what this fixture actually IS. The previous round's docstrings called
    it "a real ~18 KB markdown dossier" while it was ~1 KB, which mattered
    because the whole rule under test turned on a word count nobody had run."""
    text = _illustrated_dossier()
    assert len(text.encode("utf-8")) == 1075
    assert _body_prose_word_count(text) == 134


def test_illustrated_markdown_deliverable_keeps_its_terminal_verdict():
    """The regression this fix exists for: on two production runs a complete
    markdown dossier — its full text in the payload — was forced to
    not_checkable because it embedded `![alt](photos/8c5300c9.jpg)`, and a real
    terminal verdict was discarded. The document is checkable on its text.

    The verdict is KEPT (that is the fix). The score is capped, because no text
    rule separates this dossier from a chat agent handing over a logo with 82
    words of claims about it — both embed an image and both carry body prose. So
    the number says what was actually established: the text was read, the
    pictures were not.
    """
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "writer", output_inline=_illustrated_dossier(), end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.9, "reasoning": "all sections present"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "ok"      # not discarded — the fix
    assert verdict.terminal_judge_score == 0.85        # ...but not full verification


def test_a_verbose_image_only_handoff_cannot_earn_a_confident_pass():
    """The residual of the same class: a chat agent shipping only a picture.

    82 words of prose, every sentence a claim ABOUT the file, plus one embedded
    image — that satisfies every structural test the illustrated dossier does,
    so classification alone can never separate them. What can be said honestly is
    that in BOTH cases the pictures were never opened, so neither may carry a
    full-confidence pass. Without the cap this persisted 0.95 for work nobody saw.
    """
    handoff = (
        "I have finished the logo you asked for. The palette is a deep indigo "
        "with a warm amber accent, chosen to stay legible when the mark is "
        "reduced to favicon size. The wordmark sits to the right of the glyph "
        "and uses a geometric sans so it holds up in print as well as on screen. "
        "I exported it at three resolutions and checked the contrast ratio "
        "against the light and dark backgrounds you sent over earlier today. "
        "The file is saved and ready for review whenever you have a moment.\n\n"
        "![the finished logo](assets/logo.png)"
    )
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [make_run(1, "orch"), make_run(2, "designer", output_inline=handoff, end_time=2.0)],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.95, "reasoning": "logo delivered as asked"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_score == 0.85
    assert "not inspected" in (verdict.terminal_judge_reasoning or "")


def test_illustrated_deliverable_states_the_images_were_not_inspected():
    """Checkable is not "fully verified": the photos may show what the prose
    does not, so the reasoning carries the limit instead of an unspoken claim."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "writer", output_inline=_illustrated_dossier(), end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.9, "reasoning": "all sections present"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    reasoning = verdict.terminal_judge_reasoning
    assert "all sections present" in reasoning
    assert "text only" in reasoning
    assert "photos/8c5300c9b9545da4.jpg" in reasoning
    # The limit is DATA, not only a sentence: nothing downstream can key off a
    # suffix appended to a reasoning string, so a partial verdict records the
    # flag as well. SOFT — an illustrated dossier is not an incident.
    assert FLAG_UNINSPECTED_MEDIA in verdict.flags
    assert verdict.flagged is False


def test_bad_verdict_on_an_illustrated_deliverable_survives_too():
    """The caveat qualifies the verdict, it does not replace it: a bad terminal
    over a readable document still flags and still pages tier2."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "writer", output_inline=_illustrated_dossier(), end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "bad", "score": 0.2, "reasoning": "valuation invented"}
    )
    streams = run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "bad"
    assert verdict.flagged is True
    assert len(streams.messages(STREAM_GRAPHS_TIER2)) == 1


def test_deliverable_that_only_references_a_docx_stays_not_checkable():
    """The protection the opacity rule exists for. A payload that merely claims
    a document was written is a description; grading it would grade the claim,
    not the work — and the judge's confident "ok" must not survive that."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(
                    2,
                    "writer",
                    output_inline="Task complete. See the attached out/report.docx.",
                    end_time=2.0,
                ),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.95, "reasoning": "looks complete"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "not_checkable"
    assert verdict.terminal_judge_score is None
    assert "out/report.docx" in verdict.terminal_judge_reasoning


def test_deliverable_that_is_only_an_image_stays_not_checkable():
    """Media is not blanket-exempt: when the deliverable IS the picture the
    payload is a one-line announcement and there is still nothing to grade."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(
                    2,
                    "designer",
                    output_inline="Logo generated and saved to assets/logo.png",
                    end_time=2.0,
                ),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.95, "reasoning": "logo looks great"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "not_checkable"
    assert "assets/logo.png" in verdict.terminal_judge_reasoning


def test_verbose_deliverable_that_is_only_an_image_stays_not_checkable():
    """The false CERTAINTY a bare word-count bar bought, at the terminal layer.
    Sixty words is a normal length for an LLM's final answer, so an image-only
    hand-off that talks about its work — 87 body words here, all of them a claim
    ABOUT the file — cleared the bar and came back verdict=ok, score=0.9 on a
    picture nobody opened. The previous test pins only the one-line variant, so
    it read as protecting the media class while protecting half of it."""
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(
                    2,
                    "designer",
                    output_inline=(
                        "I have finished the logo. It uses a deep indigo as the "
                        "primary colour with a warm sand accent, chosen so the mark "
                        "reads well at small sizes and keeps enough contrast in the "
                        "reversed variant. The mark itself is a stylised compass "
                        "rose whose needle forms the letter A. I exported it at 512 "
                        "pixels and at 2048 pixels so it can be used both in the app "
                        "header and on printed material. The palette is documented "
                        "in the file header for anyone who needs to rebuild it "
                        "later. The file is saved to assets/logo.png."
                    ),
                    end_time=2.0,
                ),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.9, "reasoning": "the palette is coherent"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "not_checkable"
    assert verdict.terminal_judge_score is None
    assert "assets/logo.png" in verdict.terminal_judge_reasoning
    # Withheld, not partially verified: nothing here was read except claims.
    assert FLAG_UNINSPECTED_MEDIA not in verdict.flags


def test_photo_gallery_deliverable_is_not_verified_on_its_captions():
    """A twelve-image markdown gallery is embedded and carries 72 words of alt
    text — real production shape, and it used to clear the prose bar on caption
    words alone and be graded 'ok'. The captions describe pictures nobody
    opened; there is no body to verify, so the verdict is withheld."""
    gallery = "\n\n".join(
        f"![Front elevation seen from the street](photos/img_{i:03d}.jpg)"
        for i in range(12)
    )
    repo = FakeRepo()
    repo.add_bundle(
        make_bundle(
            [
                make_run(1, "orch"),
                make_run(2, "photographer", output_inline=gallery, end_time=2.0),
            ],
            [(1, 2)],
        )
    )
    judge = FakeJudge(
        terminal={"verdict": "ok", "score": 0.88, "reasoning": "all elevations covered"}
    )
    run_tier1(repo, judge=judge)
    verdict = _verdict(repo)
    assert verdict.terminal_judge_verdict == "not_checkable"
    assert verdict.terminal_judge_score is None
