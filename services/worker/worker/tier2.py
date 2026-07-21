"""Tier 2: full per-node scoring, blame and incident materialization
(build spec section 4.3).

Consumes ``ad.graphs.tier2`` (group ``tier2``). For each claimed job it scores
every node, runs ``find_blame``, enriches the report with fact-propagation
evidence, and persists the node scores + incident + versioned blame report in a
single Postgres transaction. The message is XACKed only after that commit, so
double-processing the same graph yields exactly one incident (the ON CONFLICT
job claim and the unique ``(graph_id, incident_key)`` constraint both enforce
it).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from uuid import UUID

from blame_engine import BlameReport, NodeScore, TerminalVerdict, find_blame

from .config import Settings
from .graph_ops import build_blame_input, build_config
from .judge_client import JudgeClient, judge_json_with_retries
from .repository import Repo
from .scoring import load_prompt, render_prompt, score_node, truncate_for_judge
from .store import ObjectStore, resolve_payload
from .streams import StreamConsumer, StreamPublisher
from .types import (
    GROUP_TIER2,
    STREAM_GRAPHS_TIER2,
    STREAM_INCIDENTS_CREATED,
    BlameDraft,
    GraphBundle,
    NodeScoreRow,
    RunRecord,
    Tier2Message,
)

logger = logging.getLogger(__name__)

_QUALITY_REPORTS = {
    "cut_point",
    "multi_culprit",
    "composition_failure",
    "root_cause_external",
}
_WORD_RE = re.compile(r"\w+")


def classify_incident(
    report_type: str, flags: list[str], terminal_bad: bool
) -> tuple[str | None, str | None]:
    """Map a blame report + tier1 flags to an ``(incident_key, trigger)``.

    Blame classification wins for quality issues (so the flagship silent
    hallucination becomes a ``degraded_quality`` incident, not a terminal
    failure). Returns ``(None, None)`` when there is nothing to open an
    incident for (unclassified report with healthy scores).
    """
    if report_type == "loop_detected" or "loop_anomaly" in flags:
        return "loop_detected", "loop_detected"
    if report_type in _QUALITY_REPORTS:
        return "degraded_quality", "degraded_quality"
    if "failed_runs" in flags:
        return "terminal_failure", "terminal_failure"
    if "cost_overrun" in flags:
        return "cost_overrun", "cost_overrun"
    if terminal_bad:
        return "terminal_failure", "terminal_failure"
    return None, None


def _claim_matches(claim: str, text: str) -> bool:
    """Substring match, falling back to majority word overlap."""
    c = claim.lower().strip()
    if not c:
        return False
    t = text.lower()
    if c in t:
        return True
    words = [w for w in _WORD_RE.findall(c) if len(w) > 3]
    if not words:
        return False
    hits = sum(1 for w in words if w in t)
    return hits / len(words) >= 0.6


def serialize_evidence(report: BlameReport, fact_propagation: list[dict] | None) -> dict:
    """Serialize a blame Evidence dataclass to a JSONB-friendly dict."""
    evidence = asdict(report.evidence)
    evidence["fact_propagation"] = fact_propagation
    return evidence


class Tier2Processor:
    """Score, blame, enrich and persist one graph."""

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
        self._judge_prompt = load_prompt("judge.md")
        self._claims_prompt = load_prompt("claims.md")
        self._weights = {
            "schema": settings.score_w_schema,
            "judge": settings.score_w_judge,
            "heuristics": settings.score_w_heuristics,
        }

    async def _payloads(
        self, run: RunRecord
    ) -> tuple[str | None, str | None]:
        input_text = await resolve_payload(
            self._store, run.input_inline, run.input_overflow_ref
        )
        output_text = await resolve_payload(
            self._store, run.output_inline, run.output_overflow_ref
        )
        return input_text, output_text

    async def _score_graph(
        self, bundle: GraphBundle, baselines, contracts, semaphore
    ) -> tuple[dict[str, NodeScore], dict[UUID, str | None]]:
        payloads = {r.run_id: await self._payloads(r) for r in bundle.runs}
        outputs = {rid: out for rid, (_inp, out) in payloads.items()}

        async def _one(run: RunRecord) -> NodeScore:
            input_text, output_text = payloads[run.run_id]
            return await score_node(
                run,
                input_text,
                output_text,
                contracts,
                baselines.get(run.agent_name),
                self._judge,
                semaphore,
                self._weights,
                self._settings.score_min_weight,
                self._judge_prompt,
                error_span_ids=[] if run.status != "failed" else ["failed"],
            )

        results = await asyncio.gather(*(_one(r) for r in bundle.runs))
        scores = {str(r.run_id): ns for r, ns in zip(bundle.runs, results)}
        return scores, outputs

    async def _fact_propagation(
        self,
        report: BlameReport,
        outputs: dict[UUID, str | None],
        agent_names: dict[str, str],
    ) -> list[dict] | None:
        if not report.culprit_run_ids:
            return None
        culprit = report.culprit_run_ids[0]
        culprit_output = outputs.get(UUID(culprit))
        if not culprit_output:
            return None
        prompt = render_prompt(
            self._claims_prompt,
            {
                "AGENT_NAME": agent_names.get(culprit, "unknown"),
                "NODE_OUTPUT": truncate_for_judge(culprit_output),
            },
        )
        result = await judge_json_with_retries(self._judge, prompt)
        if not result:
            return None
        raw_claims = result.get("claims")
        if not isinstance(raw_claims, list):
            return None
        claims = [c for c in raw_claims if isinstance(c, str) and c.strip()][:5]
        downstream = [rid for rid in report.propagation_path if rid != culprit]
        propagation: list[dict] = []
        for claim in claims:
            found_in = [
                rid
                for rid in downstream
                if (out := outputs.get(UUID(rid))) and _claim_matches(claim, out)
            ]
            propagation.append({"claim": claim, "found_in": found_in})
        return propagation

    async def process(self, message: Tier2Message) -> None:
        graph_id = UUID(message.graph_id)
        claim = await self._repo.claim_tier2_job(
            graph_id, message.dedup_key, message.trigger
        )
        if not claim.claimed:
            logger.info(
                "tier2: job %s already %s; skipping", message.dedup_key, claim.status
            )
            return

        try:
            bundle = await self._repo.load_graph(graph_id)
            if bundle is None:
                logger.warning("tier2: graph %s not found", graph_id)
                await self._repo.persist_tier2_result(
                    dedup_key=message.dedup_key,
                    node_scores=[],
                    graph_id=graph_id,
                    incident_key=None,
                    incident_trigger=None,
                    blame=None,
                )
                return

            contracts = await self._repo.read_output_contracts()
            baselines = await self._repo.read_agent_stats(bundle.graph_type)
            tier1 = await self._repo.read_tier1_verdict(graph_id)

            semaphore = asyncio.Semaphore(self._settings.judge_concurrency)
            scores, outputs = await self._score_graph(
                bundle, baselines, contracts, semaphore
            )

            node_scores = [
                NodeScoreRow(
                    run_id=r.run_id,
                    quality_score=scores[str(r.run_id)].score,
                    score_components=scores[str(r.run_id)].components,
                    unscored_reason=scores[str(r.run_id)].unscored_reason,
                    input_flawed=scores[str(r.run_id)].input_flawed,
                )
                for r in bundle.runs
            ]

            terminal_verdict: TerminalVerdict | None = None
            flags: list[str] = []
            if tier1 is not None:
                flags = list(tier1.flags)
                if tier1.terminal_judge_verdict in ("ok", "bad"):
                    terminal_verdict = TerminalVerdict(
                        bad=tier1.terminal_judge_verdict == "bad",
                        score=tier1.terminal_judge_score,
                        reasoning=tier1.terminal_judge_reasoning,
                    )

            config = build_config(
                threshold=self._settings.blame_threshold,
                gap_threshold=self._settings.gap_threshold,
                min_drop=self._settings.min_drop,
                max_loop_iterations=self._settings.max_loop_iterations,
            )
            blame_input = build_blame_input(
                bundle, scores, terminal_verdict, baselines, config
            )
            report = find_blame(blame_input)

            agent_names = {str(r.run_id): (r.agent_name or "unknown") for r in bundle.runs}
            fact_propagation = await self._fact_propagation(report, outputs, agent_names)

            terminal_bad = terminal_verdict is not None and terminal_verdict.bad
            incident_key, incident_trigger = classify_incident(
                report.report_type, flags, terminal_bad
            )

            blame_draft = None
            if incident_key is not None:
                blame_draft = BlameDraft(
                    report_type=report.report_type,
                    culprit_run_ids=[UUID(r) for r in report.culprit_run_ids],
                    propagation_path=[UUID(r) for r in report.propagation_path],
                    confidence=report.confidence,
                    downstream_cost_usd=report.downstream_cost_usd,
                    unscored_run_ids=[UUID(r) for r in report.unscored_run_ids],
                    evidence=serialize_evidence(report, fact_propagation),
                )

            outcome = await self._repo.persist_tier2_result(
                dedup_key=message.dedup_key,
                node_scores=node_scores,
                graph_id=graph_id,
                incident_key=incident_key,
                incident_trigger=incident_trigger,
                blame=blame_draft,
            )
        except Exception as exc:
            logger.exception("tier2: processing %s failed", graph_id)
            await self._repo.fail_tier2_job(message.dedup_key, str(exc))
            raise

        if outcome.incident_id is not None:
            await self._publisher.xadd_json(
                STREAM_INCIDENTS_CREATED,
                {
                    "schema_version": 1,
                    "incident_id": outcome.incident_id,
                    "graph_id": message.graph_id,
                    "blame_report_id": outcome.blame_report_id,
                    "is_new": outcome.is_new,
                },
            )
        logger.info(
            "tier2 graph=%s report=%s incident=%s is_new=%s",
            graph_id,
            report.report_type,
            outcome.incident_id,
            outcome.is_new,
        )


def parse_tier2_message(data: dict) -> Tier2Message | None:
    graph_id = data.get("graph_id")
    if not graph_id:
        return None
    return Tier2Message(
        graph_id=graph_id,
        trigger=data.get("trigger") or "tier1",
        dedup_key=data.get("dedup_key") or graph_id,
        tier1_verdict_ref=data.get("tier1_verdict_ref"),
        requested_at=data.get("requested_at"),
    )


async def run_tier2(
    consumer: StreamConsumer,
    processor: Tier2Processor,
    settings: Settings,
    *,
    stop: "object | None" = None,
) -> None:
    """Consumer loop for ``ad.graphs.tier2`` (group ``tier2``)."""
    await consumer.ensure_group(STREAM_GRAPHS_TIER2, GROUP_TIER2)
    while stop is None or not stop.is_set():
        messages = await consumer.read(
            STREAM_GRAPHS_TIER2,
            GROUP_TIER2,
            settings.consumer_name,
            settings.stream_batch_size,
            settings.stream_block_ms,
        )
        for message in messages:
            parsed = parse_tier2_message(message.data)
            try:
                if parsed is not None:
                    await processor.process(parsed)
            except Exception:
                # Job already marked failed; ack so it does not hot-loop, the
                # DLQ reaper handles persistent poison messages.
                logger.exception("tier2: message %s failed", message.id)
            await consumer.ack(STREAM_GRAPHS_TIER2, GROUP_TIER2, message.id)
