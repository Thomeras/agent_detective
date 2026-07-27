"""The in-process pipeline: tier1, the handoff, tier2, and the verdict."""

from __future__ import annotations

import asyncio

import pytest

from detective_cli.analyze import analyze, analyze_bundles, local_settings
from detective_cli.bundle import bundles_from_exports
from detective_cli.judge import NullJudge, select_judge
from worker.config import Settings

from conftest import StubJudge, judging, linear_pipeline

BAD_WRITER = {
    "writer": {
        "task_score": 0.15,
        "input_flawed": False,
        "reasoning": "the draft contradicts the brief",
    }
}
BAD_TERMINAL = {
    "verdict": "bad",
    "score": 0.2,
    "reasoning": "the deliverable does not answer the request",
}


def run_pipeline(judge: StubJudge, *, payload=None, **kwargs):
    bundles = bundles_from_exports([payload or linear_pipeline()])
    return asyncio.run(
        analyze_bundles(bundles, local_settings(), judge=judging(judge), **kwargs)
    )


class TestJudgedChannel:
    def test_a_bad_node_becomes_a_localized_incident(self):
        graphs = run_pipeline(
            StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL)
        )
        graph = graphs[0]
        assert graph.incident is not None
        report = graph.blame_report
        assert report is not None
        assert report["report_type"] == "cut_point"
        culprits = {graph.agent_names[str(c)] for c in report["culprit_run_ids"]}
        assert culprits == {"writer"}

    def test_a_healthy_run_raises_no_incident(self):
        graphs = run_pipeline(StubJudge())
        assert graphs[0].incident is None
        assert graphs[0].clean

    def test_every_node_is_scored_when_the_judge_answers(self):
        graphs = run_pipeline(StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL))
        scores = graphs[0].node_scores
        assert len(scores) == 3
        assert all(row.quality_score is not None for row in scores.values())

    def test_the_blamed_node_carries_the_lowest_score(self):
        graphs = run_pipeline(StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL))
        graph = graphs[0]
        by_agent = {
            graph.agent_names[str(run_id)]: row.quality_score
            for run_id, row in graph.node_scores.items()
        }
        assert by_agent["writer"] == min(by_agent.values())


class TestWithoutAJudge:
    def test_nodes_are_unscored_rather_than_presumed_healthy(self):
        # The whole honesty argument: no judge means no verdict, not a pass.
        bundles = bundles_from_exports([linear_pipeline()])
        graphs = asyncio.run(
            analyze_bundles(
                bundles,
                local_settings(),
                judge=select_judge(Settings(judge_base_url=""), force_off=True),
            )
        )
        rows = graphs[0].node_scores
        assert rows, "tier2 still runs; it simply has nothing to score with"
        assert all(row.quality_score is None for row in rows.values())
        assert all(
            row.unscored_reason == "insufficient_components" for row in rows.values()
        )

    def test_no_incident_is_invented_from_the_absence_of_a_judge(self):
        bundles = bundles_from_exports([linear_pipeline()])
        graphs = asyncio.run(
            analyze_bundles(
                bundles,
                local_settings(),
                judge=select_judge(Settings(judge_base_url=""), force_off=True),
            )
        )
        assert graphs[0].incident is None

    def test_the_null_judge_does_not_retry(self):
        # A permanent failure must not cost three attempts and two backoffs per
        # node; that latency is pure waste on the default code path.
        judge = NullJudge()
        calls = {"n": 0}

        async def counted(prompt, *, system=None):
            calls["n"] += 1
            return await NullJudge.complete_json(judge, prompt, system=system)

        judge.complete_json = counted  # type: ignore[method-assign]
        from worker.judge_client import judge_json_with_retries

        assert asyncio.run(judge_json_with_retries(judge, "prompt")) is None
        assert calls["n"] == 1


class TestTierHandoff:
    def test_tier1_only_skips_scoring_entirely(self):
        graphs = run_pipeline(
            StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL), tier1_only=True
        )
        graph = graphs[0]
        assert graph.tier2_ran is False
        assert graph.node_scores == {}
        assert graph.blame_report is None

    def test_tier1_still_produces_its_verdict_in_tier1_only_mode(self):
        graphs = run_pipeline(
            StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL), tier1_only=True
        )
        verdict = graphs[0].verdict
        assert verdict is not None
        assert verdict.terminal_judge_verdict == "bad"
        assert verdict.flagged is True

    def test_a_clean_graph_still_reaches_tier2_locally(self):
        # Production samples; local mode analyses the file it was pointed at.
        graphs = run_pipeline(StubJudge())
        assert graphs[0].tier2_ran is True

    def test_each_graph_is_handed_off_independently(self):
        other = linear_pipeline()
        for s in other["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            s["traceId"] = "2" * 32
        bundles = bundles_from_exports([linear_pipeline(), other])
        graphs = asyncio.run(
            analyze_bundles(
                bundles,
                local_settings(),
                judge=judging(StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL)),
            )
        )
        assert len(graphs) == 2
        assert all(g.tier2_ran for g in graphs)
        # One incident per graph — the job claim is keyed per graph, so the
        # second must not be swallowed as a duplicate of the first.
        assert all(g.incident is not None for g in graphs)


class TestDeterminism:
    def test_the_same_trace_yields_the_same_verdict(self):
        def once():
            graphs = run_pipeline(
                StubJudge(node_scores=BAD_WRITER, terminal=BAD_TERMINAL)
            )
            report = graphs[0].blame_report
            return (
                report["report_type"],
                [str(c) for c in report["culprit_run_ids"]],
                report["confidence"],
            )

        assert once() == once()

    def test_analyze_is_a_thin_synchronous_wrapper(self):
        run = analyze(
            bundles_from_exports([linear_pipeline()]), no_judge=True
        )
        assert len(run.graphs) == 1
        assert run.judge_enabled is False
        assert run.clean


class TestSettings:
    def test_local_mode_analyses_the_file_it_was_given(self):
        assert local_settings().tier2_sample_pct == 100

    def test_explicit_overrides_win(self):
        assert local_settings({"tier2_sample_pct": 0}).tier2_sample_pct == 0

    def test_engine_thresholds_keep_their_deployed_meaning(self):
        # Local mode must not quietly retune the analysis; only the sampling
        # question differs.
        local = local_settings()
        deployed = Settings()
        assert local.blame_threshold == deployed.blame_threshold
        assert local.gap_threshold == deployed.gap_threshold
        assert local.min_drop == deployed.min_drop
        assert local.score_min_weight == deployed.score_min_weight


class TestDemoTraces:
    def test_the_recorded_fault_is_blamed_on_the_scraper(self, demo_traces):
        import json

        payload = json.loads(demo_traces["faulted"].read_text(encoding="utf-8"))
        graphs = run_pipeline(
            StubJudge(
                node_scores={
                    "scraper-agent": {
                        "task_score": 0.2,
                        "input_flawed": False,
                        "reasoning": "prices the source never listed",
                    }
                },
                terminal={
                    "verdict": "bad",
                    "score": 0.2,
                    "reasoning": "the final output carries fabricated prices",
                },
            ),
            payload=payload,
        )
        graph = graphs[0]
        assert graph.incident is not None
        culprits = {graph.agent_names[str(c)] for c in graph.blame_report["culprit_run_ids"]}
        assert culprits == {"scraper-agent"}

    def test_the_recorded_happy_path_is_clean(self, demo_traces):
        import json

        payload = json.loads(demo_traces["happy"].read_text(encoding="utf-8"))
        graphs = run_pipeline(StubJudge(), payload=payload)
        assert graphs[0].incident is None
