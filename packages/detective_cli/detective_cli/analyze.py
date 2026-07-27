"""Run the real tier1/tier2 pipeline over one trace, in one process.

This is the whole point of local mode: not a reimplementation of the analysis,
but the SAME ``Tier1Processor`` and ``Tier2Processor`` the deployed worker runs,
handed in-memory implementations of the three seams they talk to. The pipeline
cannot tell the difference, which is what makes a local verdict comparable to a
deployed one.

What is replaced, and nothing else:

===================  ==========================  ==============================
seam                 deployed                    local
===================  ==========================  ==============================
``Repo``             Postgres via ``PgRepo``     ``worker.memory.InMemoryRepo``
``ObjectStore``      MinIO                       inline payloads
``StreamPublisher``  Redis Streams               ``CollectingPublisher``
``JudgeClient``      OpenAI-compatible endpoint  the same, or none at all
===================  ==========================  ==============================

The tier1 -> tier2 handoff still goes through a published message: tier1 decides
whether the graph deserves deep analysis and publishes to ``ad.graphs.tier2``;
we read what it published rather than deciding for it. Local mode only changes
the *sampling* input to that decision (see ``DEFAULT_SAMPLE_PCT``) — analyzing
one trace on purpose is not the same situation as a percentage of production
traffic, but the gate itself is untouched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from worker.config import Settings
from worker.memory import CollectingPublisher, InMemoryObjectStore, InMemoryRepo
from worker.tier1 import Tier1Processor
from worker.tier2 import Tier2Processor, parse_tier2_message
from worker.types import (
    STREAM_GRAPHS_TIER2,
    GraphBundle,
    NodeScoreRow,
    Tier1Verdict,
)

from .judge import JudgeChoice, select_judge

# Production samples a percentage of UNFLAGGED graphs into the expensive tier;
# locally the user pointed at one file and asked for the analysis, so the
# default is to run it. `--tier1-only` restores the cheap pass alone.
DEFAULT_SAMPLE_PCT = 100


@dataclass
class GraphAnalysis:
    """Everything one graph produced, as the CLI's renderers consume it."""

    graph_id: UUID
    bundle: GraphBundle
    verdict: Tier1Verdict | None
    node_scores: dict[UUID, NodeScoreRow] = field(default_factory=dict)
    incident: dict[str, Any] | None = None
    blame_report: dict[str, Any] | None = None
    tier2_ran: bool = False

    @property
    def agent_names(self) -> dict[str, str]:
        """run_id (str) -> agent name, for rendering ids as something readable."""
        return {
            str(run.run_id): run.agent_name or str(run.run_id)[:8] for run in self.bundle.runs
        }

    @property
    def node_scores_by_str(self) -> dict[str, NodeScoreRow]:
        """The score rows keyed as the evidence payload keys them (str run_id)."""
        return {str(run_id): row for run_id, row in self.node_scores.items()}

    @property
    def clean(self) -> bool:
        """True when nothing worth paging about came out of this graph.

        An incident is the signal, not the report type: tier2 writes a blame
        report for every graph it analyses, including ones it clears.
        """
        return self.incident is None


@dataclass
class AnalysisRun:
    """The result of analysing one trace file."""

    graphs: list[GraphAnalysis]
    judge: str
    judge_enabled: bool
    settings: Settings

    @property
    def incidents(self) -> list[GraphAnalysis]:
        return [g for g in self.graphs if g.incident is not None]

    @property
    def clean(self) -> bool:
        return not self.incidents


def local_settings(overrides: dict[str, Any] | None = None) -> Settings:
    """Worker settings for a local run: environment first, local defaults after.

    Every knob still comes from the same ``Settings`` the deployed worker reads,
    so a threshold tuned in production means the same thing here. Only the
    sampling percentage differs by default, and only because the question being
    asked is different.
    """
    values: dict[str, Any] = {"tier2_sample_pct": DEFAULT_SAMPLE_PCT}
    values.update(overrides or {})
    # Settings reads the environment for anything not passed explicitly.
    return Settings(**values)


async def analyze_bundles(
    bundles: list[GraphBundle],
    settings: Settings,
    *,
    judge: JudgeChoice,
    tier1_only: bool = False,
) -> list[GraphAnalysis]:
    """Drive tier1 (and tier2 where tier1 asks for it) over each bundle."""
    repo = InMemoryRepo()
    store = InMemoryObjectStore()
    publisher = CollectingPublisher()
    for bundle in bundles:
        repo.add_bundle(bundle)

    tier1 = Tier1Processor(repo, store, publisher, judge.client, settings)
    tier2 = Tier2Processor(repo, store, publisher, judge.client, settings)

    results: list[GraphAnalysis] = []
    for bundle in bundles:
        graph_id_str = str(bundle.graph_id)
        await tier1.process(graph_id_str)

        tier2_ran = False
        if not tier1_only:
            # Whatever tier1 published for THIS graph is the handoff; a graph it
            # chose not to escalate simply has no message.
            for raw in publisher.messages(STREAM_GRAPHS_TIER2):
                if raw.get("graph_id") != graph_id_str:
                    continue
                message = parse_tier2_message(raw)
                if message is None:
                    continue
                await tier2.process(message)
                tier2_ran = True

        incident = next(
            (
                inc
                for (gid, _key), inc in repo.incidents.items()
                if gid == bundle.graph_id
            ),
            None,
        )
        report = next(
            (
                b
                for b in reversed(repo.blame_reports)
                if b["graph_id"] == bundle.graph_id and b["is_latest"]
            ),
            None,
        )
        results.append(
            GraphAnalysis(
                graph_id=bundle.graph_id,
                bundle=bundle,
                verdict=repo.tier1.get(bundle.graph_id),
                node_scores={
                    run.run_id: repo.node_scores[run.run_id]
                    for run in bundle.runs
                    if run.run_id in repo.node_scores
                },
                incident=incident,
                blame_report=report,
                tier2_ran=tier2_ran,
            )
        )
    return results


async def analyze_async(
    bundles: list[GraphBundle],
    *,
    settings: Settings | None = None,
    no_judge: bool = False,
    tier1_only: bool = False,
) -> AnalysisRun:
    settings = settings or local_settings()
    judge = select_judge(settings, force_off=no_judge)
    try:
        graphs = await analyze_bundles(
            bundles, settings, judge=judge, tier1_only=tier1_only
        )
    finally:
        await judge.close()
    return AnalysisRun(
        graphs=graphs,
        judge=judge.description,
        judge_enabled=judge.enabled,
        settings=settings,
    )


def analyze(
    bundles: list[GraphBundle],
    *,
    settings: Settings | None = None,
    no_judge: bool = False,
    tier1_only: bool = False,
) -> AnalysisRun:
    """Synchronous entry point (the CLI's; the pipeline itself is async)."""
    return asyncio.run(
        analyze_async(
            bundles, settings=settings, no_judge=no_judge, tier1_only=tier1_only
        )
    )
