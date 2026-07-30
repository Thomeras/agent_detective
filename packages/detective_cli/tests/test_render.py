"""Rendering: what the report claims, and what it refuses to claim."""

from __future__ import annotations

import asyncio
import json

import pytest

from detective_cli.analyze import analyze_bundles, local_settings
from detective_cli.bundle import bundles_from_exports
from detective_cli.judge import select_judge
from detective_cli.render import (
    color_enabled,
    render_json,
    render_markdown,
    render_terminal,
    unverified_graphs,
)
from detective_cli.analyze import AnalysisRun
from worker.config import Settings

from conftest import StubJudge, judging, linear_pipeline

BAD_WRITER = {
    "writer": {"task_score": 0.15, "input_flawed": False, "reasoning": "off-brief"}
}
BAD_TERMINAL = {"verdict": "bad", "score": 0.2, "reasoning": "does not answer the request"}


def make_run(judge=None, *, no_judge: bool = False) -> AnalysisRun:
    bundles = bundles_from_exports([linear_pipeline()])
    settings = local_settings()
    choice = (
        select_judge(Settings(judge_base_url=""), force_off=True)
        if no_judge
        else judging(judge or StubJudge())
    )
    graphs = asyncio.run(analyze_bundles(bundles, settings, judge=choice))
    return AnalysisRun(
        graphs=graphs,
        judge=choice.description,
        judge_enabled=choice.enabled,
        settings=settings,
    )


@pytest.fixture
def failing_run() -> AnalysisRun:
    return make_run(StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL))


@pytest.fixture
def clean_run() -> AnalysisRun:
    return make_run(StubJudge())


@pytest.fixture
def unmeasured_run() -> AnalysisRun:
    return make_run(no_judge=True)


@pytest.fixture
def unanalyzed_run() -> AnalysisRun:
    # tier1-only: scoring never ran, so the runs carry no score rows at all.
    bundles = bundles_from_exports([linear_pipeline()])
    settings = local_settings()
    choice = judging(StubJudge())
    graphs = asyncio.run(analyze_bundles(bundles, settings, judge=choice, tier1_only=True))
    return AnalysisRun(
        graphs=graphs,
        judge=choice.description,
        judge_enabled=choice.enabled,
        settings=settings,
    )


class TestTerminal:
    def test_a_failure_names_the_verdict_and_the_culprit(self, failing_run):
        text = render_terminal(failing_run, "trace.json", color=False)
        assert "FAILED" in text
        assert "cut_point" in text
        assert "writer" in text

    def test_a_clean_run_reads_as_a_pass(self, clean_run):
        text = render_terminal(clean_run, "trace.json", color=False)
        assert "PASSED" in text
        assert "no incidents" in text

    def test_an_unmeasured_run_is_never_reported_as_a_pass(self, unmeasured_run):
        # The load-bearing case: no judge, no deterministic signal, nothing
        # measured. Calling that PASSED would assert quality from silence.
        text = render_terminal(unmeasured_run, "trace.json", color=False)
        assert "NOT VERIFIED" in text
        assert "PASSED" not in text
        assert "no incidents across" not in text

    def test_an_unmeasured_run_says_the_judged_channel_was_off(self, unmeasured_run):
        text = render_terminal(unmeasured_run, "trace.json", color=False)
        assert "judged channel: OFF" in text
        assert "JUDGE_BASE_URL" in text

    def test_unscored_nodes_show_their_reason_not_a_score(self, unmeasured_run):
        text = render_terminal(unmeasured_run, "trace.json", color=False)
        assert "unscored (insufficient_components)" in text
        assert "0.00" not in text

    def test_never_analyzed_nodes_are_named_not_analyzed(self, unanalyzed_run):
        text = render_terminal(unanalyzed_run, "trace.json", color=False)
        pipeline = text.split("Pipeline", 1)[1]
        assert "not analyzed" in pipeline
        assert "unscored" not in pipeline

    def test_node_states_breakdown_counts_each_state(self, unanalyzed_run):
        text = render_terminal(unanalyzed_run, "trace.json", color=False)
        assert "node states: 3 not_analyzed" in text

    def test_the_origin_node_is_marked_in_the_pipeline_listing(self, failing_run):
        text = render_terminal(failing_run, "trace.json", color=False)
        # Read the Pipeline section specifically — the culprit heading above it
        # also names the node, and matching that would prove nothing.
        pipeline = text.split("Pipeline", 1)[1]
        origin_line = next(
            line for line in pipeline.splitlines() if line.strip().startswith("writer")
        )
        assert "ORIGIN" in origin_line
        # The composite score is shown, not the raw judge number it blends.
        assert "drop" in origin_line

    def test_defect_cards_carry_both_confidences_and_the_channel(self, failing_run):
        text = render_terminal(failing_run, "trace.json", color=False)
        assert "observation" in text and "attribution" in text
        assert "channel judged" in text

    def test_findings_are_cited_under_their_defect(self, failing_run):
        text = render_terminal(failing_run, "trace.json", color=False)
        assert "supporting:" in text

    def test_finding_subjects_render_as_agent_names_not_run_prefixes(self, failing_run):
        text = render_terminal(failing_run, "trace.json", color=False)
        assert "run:" not in text

    def test_color_is_absent_when_disabled(self, failing_run):
        assert "\033[" not in render_terminal(failing_run, "t.json", color=False)

    def test_color_is_present_when_enabled(self, failing_run):
        assert "\033[" in render_terminal(failing_run, "t.json", color=True)

    def test_the_source_file_is_named_in_the_header(self, clean_run):
        assert "my-trace.json" in render_terminal(clean_run, "my-trace.json", color=False)


class TestColorDetection:
    def test_no_color_env_wins_over_a_tty(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")

        class Tty:
            def isatty(self):
                return True

        assert color_enabled(Tty()) is False

    def test_a_pipe_gets_no_color(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)

        class Pipe:
            def isatty(self):
                return False

        assert color_enabled(Pipe()) is False

    def test_force_overrides_everything(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")

        class Pipe:
            def isatty(self):
                return False

        assert color_enabled(Pipe(), force=True) is True


class TestUnverifiedDetection:
    def test_an_unmeasured_graph_is_reported_as_unverified(self, unmeasured_run):
        assert len(unverified_graphs(unmeasured_run)) == 1

    def test_a_measured_clean_graph_is_not_unverified(self, clean_run):
        assert unverified_graphs(clean_run) == []

    def test_a_failing_graph_is_not_unverified(self, failing_run):
        assert unverified_graphs(failing_run) == []


class TestMarkdown:
    def test_the_brief_leads_with_the_verdict(self, failing_run):
        md = render_markdown(failing_run, "trace.json")
        assert md.startswith("# Agent Detective")
        assert "**FAILED**" in md

    def test_the_pipeline_table_is_valid_markdown(self, failing_run):
        md = render_markdown(failing_run, "trace.json")
        assert "| node | score | notes |" in md
        assert "| --- | --- | --- |" in md

    def test_defects_are_listed_with_their_evidence(self, failing_run):
        md = render_markdown(failing_run, "trace.json")
        assert "### Defects" in md
        assert "supporting:" in md

    def test_the_missing_judged_channel_is_disclosed(self, unmeasured_run):
        md = render_markdown(unmeasured_run, "trace.json")
        assert "deterministic channel alone" in md
        assert "JUDGE_BASE_URL" in md

    def test_the_header_counts_unverified_graphs(self, unmeasured_run):
        assert "**Unverified:** 1" in render_markdown(unmeasured_run, "trace.json")

    def test_the_node_state_breakdown_is_listed(self, unanalyzed_run):
        md = render_markdown(unanalyzed_run, "trace.json")
        assert "Node states: 3 not_analyzed" in md


class TestJson:
    def test_the_payload_is_serialisable(self, failing_run):
        payload = render_json(failing_run, "trace.json")
        json.dumps(payload, default=str)

    def test_it_reports_the_verdict_culprits_and_incident(self, failing_run):
        payload = render_json(failing_run, "trace.json")
        graph = payload["graphs"][0]
        assert graph["verdict"] == "FAILED"
        assert graph["report_type"] == "cut_point"
        assert graph["culprits"] == ["writer"]
        assert graph["incident"] is not None
        assert payload["incidents"] == 1

    def test_it_records_whether_anything_was_measured(self, unmeasured_run, failing_run):
        assert render_json(unmeasured_run, "t")["graphs"][0]["measured"] is False
        assert render_json(failing_run, "t")["graphs"][0]["measured"] is True

    def test_it_records_the_judge_that_ran(self, unmeasured_run):
        assert render_json(unmeasured_run, "t")["judge"]["enabled"] is False

    def test_the_full_evidence_payload_is_included(self, failing_run):
        evidence = render_json(failing_run, "t")["graphs"][0]["evidence"]
        assert evidence["schema"] == 2
        assert evidence["defects"]
        assert evidence["findings"]
