"""Tier 1: cheap, always-on detection (build spec section 4.2).

Consumes ``ad.graphs.completed`` (group ``tier1``). For each finalized graph it
computes deterministic flags (failed runs, cost overrun, loop anomaly, schema
violation, degenerate terminal output) and makes exactly one terminal judge
call over the full terminal output — that single call is what catches silent
hallucinations where every run reported ``status=ok``.

It upserts ``tier1_verdicts`` (PK graph_id, idempotent) and, when the graph is
flagged or sampled, publishes ``ad.graphs.tier2`` with ``dedup_key=graph_id``.
The Postgres upsert commits before the stream publish and the message is only
XACKed after both succeed.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from blame_engine import detect_loop_anomalies

from .config import Settings
from .graph_ops import build_config, build_loop_input, root_run, terminal_run
from .judge_client import JudgeClient, judge_json_with_retries
from .repository import Repo
from .scoring import (
    evaluate_schema,
    is_degenerate_output,
    load_prompt,
    render_prompt,
    truncate_for_judge,
)
from .store import ObjectStore, resolve_payload
from .streams import StreamConsumer, StreamPublisher
from .types import (
    FLAG_COST_OVERRUN,
    FLAG_DEGENERATE_OUTPUT,
    FLAG_FAILED_RUNS,
    FLAG_LOOP_ANOMALY,
    FLAG_SCHEMA_VIOLATION,
    GROUP_TIER1,
    STREAM_GRAPHS_COMPLETED,
    STREAM_GRAPHS_TIER2,
    GraphBundle,
    RunRecord,
    Tier1Verdict,
)

logger = logging.getLogger(__name__)


def _graph_cost(bundle: GraphBundle) -> float:
    if bundle.total_cost_usd is not None:
        return bundle.total_cost_usd
    return sum(r.cost_usd or 0.0 for r in bundle.runs)


def _samples(graph_id: str, sample_pct: int) -> bool:
    if sample_pct <= 0:
        return False
    if sample_pct >= 100:
        return True
    digest = hashlib.sha256(graph_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100 < sample_pct


class Tier1Processor:
    """Deterministic flags + one terminal judge call per finalized graph."""

    def __init__(
        self,
        repo: Repo,
        store: ObjectStore,
        publisher: StreamPublisher,
        judge: JudgeClient,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._store = store
        self._publisher = publisher
        self._judge = judge
        self._settings = settings
        self._terminal_prompt = load_prompt("terminal_judge.md")

    async def _output_text(self, run: RunRecord) -> str | None:
        return await resolve_payload(self._store, run.output_inline, run.output_overflow_ref)

    async def _input_text(self, run: RunRecord) -> str | None:
        return await resolve_payload(self._store, run.input_inline, run.input_overflow_ref)

    async def _terminal_judge(
        self, bundle: GraphBundle, terminal_output: str | None
    ) -> tuple[str, float | None, str | None]:
        """Return (verdict, score, reasoning); verdict in {ok, bad, error}."""
        root = root_run(bundle)
        graph_input = await self._input_text(root) if root is not None else None
        goal = bundle.name or bundle.graph_type or "complete the requested task"
        prompt = render_prompt(
            self._terminal_prompt,
            {
                "GRAPH_GOAL": goal,
                "GRAPH_INPUT": truncate_for_judge(graph_input or ""),
                "TERMINAL_OUTPUT": truncate_for_judge(terminal_output or ""),
            },
        )
        verdict = await judge_json_with_retries(self._judge, prompt)
        if verdict is None:
            return "error", None, None
        raw_verdict = verdict.get("verdict")
        normalized = "bad" if raw_verdict == "bad" else "ok" if raw_verdict == "ok" else "error"
        score = verdict.get("score")
        score_val = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        reasoning = verdict.get("reasoning")
        reasoning_val = reasoning if isinstance(reasoning, str) else None
        return normalized, score_val, reasoning_val

    async def process(self, graph_id_str: str) -> None:
        graph_id = UUID(graph_id_str)
        bundle = await self._repo.load_graph(graph_id)
        if bundle is None:
            logger.warning("tier1: graph %s not found; skipping", graph_id)
            return
        contracts = await self._repo.read_output_contracts()
        baselines = await self._repo.read_agent_stats(bundle.graph_type)

        outputs = {r.run_id: await self._output_text(r) for r in bundle.runs}

        flags: list[str] = []
        if any(r.status == "failed" for r in bundle.runs):
            flags.append(FLAG_FAILED_RUNS)

        budget = self._settings.cost_budget_default_usd
        if budget is not None and _graph_cost(bundle) > budget:
            flags.append(FLAG_COST_OVERRUN)

        config = build_config(
            threshold=self._settings.blame_threshold,
            gap_threshold=self._settings.gap_threshold,
            min_drop=self._settings.min_drop,
            max_loop_iterations=self._settings.max_loop_iterations,
        )
        if detect_loop_anomalies(build_loop_input(bundle, baselines, config)):
            flags.append(FLAG_LOOP_ANOMALY)

        if any(
            evaluate_schema(outputs[r.run_id], contracts, r.agent_name, r.agent_version) == 0.0
            for r in bundle.runs
        ):
            flags.append(FLAG_SCHEMA_VIOLATION)

        terminal = terminal_run(bundle)
        terminal_output = outputs.get(terminal.run_id) if terminal is not None else None
        if is_degenerate_output(terminal_output):
            flags.append(FLAG_DEGENERATE_OUTPUT)

        verdict, score, reasoning = await self._terminal_judge(bundle, terminal_output)

        flagged = bool(flags) or verdict == "bad"
        sampled = not flagged and _samples(graph_id_str, self._settings.tier2_sample_pct)

        await self._repo.upsert_tier1_verdict(
            Tier1Verdict(
                graph_id=graph_id,
                terminal_judge_verdict=verdict,
                terminal_judge_score=score,
                terminal_judge_reasoning=reasoning,
                flags=flags,
                flagged=flagged,
                sampled=sampled,
            )
        )

        if flagged or sampled:
            await self._publisher.xadd_json(
                STREAM_GRAPHS_TIER2,
                {
                    "schema_version": 1,
                    "graph_id": graph_id_str,
                    "trigger": "tier1" if flagged else "sampled",
                    "dedup_key": graph_id_str,
                    "tier1_verdict_ref": graph_id_str,
                    "requested_at": None,
                },
            )
        logger.info(
            "tier1 graph=%s verdict=%s flags=%s flagged=%s sampled=%s",
            graph_id,
            verdict,
            flags,
            flagged,
            sampled,
        )


async def run_tier1(
    consumer: StreamConsumer,
    processor: Tier1Processor,
    settings: Settings,
    *,
    stop: "object | None" = None,
) -> None:
    """Consumer loop for ``ad.graphs.completed`` (group ``tier1``)."""
    await consumer.ensure_group(STREAM_GRAPHS_COMPLETED, GROUP_TIER1)
    while stop is None or not stop.is_set():
        messages = await consumer.read(
            STREAM_GRAPHS_COMPLETED,
            GROUP_TIER1,
            settings.consumer_name,
            settings.stream_batch_size,
            settings.stream_block_ms,
        )
        for message in messages:
            graph_id = message.data.get("graph_id")
            try:
                if graph_id:
                    await processor.process(graph_id)
            except Exception:
                logger.exception("tier1: processing %s failed", graph_id)
                continue
            await consumer.ack(STREAM_GRAPHS_COMPLETED, GROUP_TIER1, message.id)
