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

from .behavioral import (
    cost_zscore_signals,
    duplicate_side_effect_signals,
    loop_fingerprint_signals,
    parse_tool_calls,
    retry_storm_signals,
    tool_args_signals,
)
from .checks_content import required_section_signals
from .checks_security import injection_signature_signals, sensitive_data_signals
from .config import Settings
from .graph_ops import build_config, build_loop_input, deliverable_run, root_run
from .judge_client import JudgeClient, judge_json_with_retries
from .policy import judge_prompts_fingerprint
from .repository import Repo
from .scoring import (
    evaluate_schema,
    is_degenerate_output,
    load_prompt,
    opaque_artifact_refs,
    render_prompt,
    truncate_for_judge,
)
from .signals import artifact_integrity_signals, check_rules_fingerprint
from .store import ObjectStore, resolve_payload
from .streams import StreamConsumer, StreamPublisher
from .types import (
    FLAG_ARTIFACT_INTEGRITY,
    FLAG_COST_OVERRUN,
    FLAG_DEGENERATE_OUTPUT,
    FLAG_FAILED_RUNS,
    FLAG_LOOP_ANOMALY,
    FLAG_REQUIRED_SECTION,
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
        """Return (verdict, score, reasoning); verdict in {ok, bad, not_checkable, error}.

        ``terminal_output`` must be the DELIVERABLE (see ``deliverable_run``), not
        the orchestrator root's empty output or a verifier's PASS/FAIL verdict.

        Honesty guard: if the deliverable content is not actually visible — the
        payload is empty, or it only *references* a binary artifact (docx/pdf/…)
        whose text was never embedded — we cannot confirm OR deny the goal.
        Whatever the LLM guessed is overridden to ``not_checkable``. A confident
        "the final output is completely empty" over an unseen file is exactly the
        false-certainty this project exists to prevent.
        """
        # Deterministic, before trusting any LLM claim about content we may not
        # have shown it.
        if not (terminal_output or "").strip():
            return "not_checkable", None, "deliverable payload was empty or absent"
        opaque = opaque_artifact_refs(terminal_output)

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
        normalized = (
            "bad" if raw_verdict == "bad"
            else "ok" if raw_verdict == "ok"
            else "not_checkable" if raw_verdict == "not_checkable"
            else "error"
        )
        score = verdict.get("score")
        score_val = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        reasoning = verdict.get("reasoning")
        reasoning_val = reasoning if isinstance(reasoning, str) else None
        # Override: the artifact content was never in the payload, so a bad/ok
        # verdict is a guess about an unopened file. Force not_checkable.
        if opaque:
            return (
                "not_checkable",
                None,
                "deliverable references a file artifact whose content was not "
                f"embedded ({', '.join(opaque[:3])}) — cannot verify the goal",
            )
        return normalized, score_val, reasoning_val

    async def process(self, graph_id_str: str) -> None:
        graph_id = UUID(graph_id_str)
        bundle = await self._repo.load_graph(graph_id)
        if bundle is None:
            logger.warning("tier1: graph %s not found; skipping", graph_id)
            return
        contracts = await self._repo.read_output_contracts()
        baselines = await self._repo.read_agent_stats(bundle.graph_type)
        check_rules = await self._repo.read_check_rules()

        outputs = {r.run_id: await self._output_text(r) for r in bundle.runs}

        # HARD flags mark a defect and set flagged=True (triggering tier2).
        # SOFT flags are recorded observations (anomalies, exposure warnings)
        # that ride along in the verdict but must NOT page on their own —
        # a contact email in a marketing deliverable is not an incident.
        flags: list[str] = []
        soft_flags: list[str] = []
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

        # Grade the actual deliverable, not the orchestrator root's empty output
        # or a verifier's verdict (see deliverable_run — a retry loopback removes
        # the graph's only sink and the naive fallback picks the root wrapper).
        deliverable = deliverable_run(bundle)
        terminal_output = outputs.get(deliverable.run_id) if deliverable is not None else None
        if is_degenerate_output(terminal_output):
            flags.append(FLAG_DEGENERATE_OUTPUT)

        # Deterministic artifact integrity (docs/deterministic-signals.md A1):
        # re-check the deliverable's OUT-OF-BAND artifact_meta attribute (the
        # agent_runs column, never the payload text — payload text is forgeable
        # by document content). A failed check is ground truth — a corrupt/
        # missing/mis-typed artifact IS a bad terminal, so the verdict is set
        # here deterministically and the LLM judge is skipped entirely (cost 0,
        # and it cannot be fooled by fluent claims).
        integrity_fails = [
            s
            for s in artifact_integrity_signals(
                deliverable.artifact_meta if deliverable is not None else None,
                min_bytes=self._settings.min_artifact_bytes,
            )
            if s["severity"] == "fail"
        ]
        # Registered required sections on the deliverable (check_rules): a
        # registered requirement physically absent from the deliverable text is
        # the same class of ground truth — "the budget table is missing" is
        # checkable without a judge.
        section_fails: list[dict] = []
        if deliverable is not None:
            section_rules = [
                r.spec
                for r in check_rules
                if r.kind == "required_section"
                and r.agent_name in (None, deliverable.agent_name)
                and r.graph_type in (None, bundle.graph_type)
            ]
            section_fails = [
                s
                for s in required_section_signals(terminal_output, section_rules)
                if s["severity"] == "fail"
            ]
            if section_fails:
                flags.append(FLAG_REQUIRED_SECTION)

        # Behavioral + cost/token signals per run (tool-call digest + rolling
        # baselines). duplicate_side_effect and tool_args_invalid are HARD (a
        # payment posted twice is an incident); fingerprint/retry/cost/token
        # anomalies and security scans are SOFT observations.
        tool_schemas = [r.spec for r in check_rules if r.kind == "tool_schema"]
        for r in bundle.runs:
            calls = parse_tool_calls(r.tool_calls)
            hard = duplicate_side_effect_signals(calls) + tool_args_signals(
                r.tool_calls, calls, tool_schemas
            )
            soft = (
                loop_fingerprint_signals(calls)
                + retry_storm_signals(calls)
                + cost_zscore_signals(
                    r.agent_name or "unknown",
                    cost=r.cost_usd,
                    tokens_out=float(r.tokens_out) if r.tokens_out is not None else None,
                    stat=baselines.get(r.agent_name),
                )
                + sensitive_data_signals(outputs.get(r.run_id))
                + sensitive_data_signals(r.input_inline)
                + injection_signature_signals(outputs.get(r.run_id))
                + injection_signature_signals(r.input_inline)
            )
            for s in hard:
                if s["severity"] == "fail" and s["name"] not in flags:
                    flags.append(s["name"])
            for s in soft:
                if s["name"] not in soft_flags:
                    soft_flags.append(s["name"])

        if integrity_fails or section_fails:
            # Deterministic ground truth about the deliverable: the verdict is
            # bad at score 0.0 and the LLM judge is skipped entirely (cost 0,
            # and it cannot be fooled by fluent claims).
            if integrity_fails:
                flags.append(FLAG_ARTIFACT_INTEGRITY)
            details = "; ".join(
                f"{s['detail']} ({s['basis']})"
                for s in integrity_fails + section_fails
            )
            verdict, score = "bad", 0.0
            reasoning = f"deterministic deliverable check failure: {details}"
        else:
            verdict, score, reasoning = await self._terminal_judge(bundle, terminal_output)

        flagged = bool(flags) or verdict == "bad"
        # Soft flags are persisted for evidence/visibility but do not page.
        flags = flags + [f for f in soft_flags if f not in flags]
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
                # Provenance for later reconciliation: the exact rule-set
                # fingerprint this verdict's deterministic basis ran under.
                check_rules_hash=check_rules_fingerprint(
                    check_rules,
                    min_artifact_bytes=self._settings.min_artifact_bytes,
                ),
                # The worker's OWN judge-prompt fingerprint (migration 0009) so
                # calibration can slice verdicts by judge-prompt version. The
                # judge MODEL is not recorded — a known limitation.
                judge_prompt_hash=judge_prompts_fingerprint(),
            )
        )

        # Rolling baselines (the writer the cost/token anomaly check needs —
        # until now agent_stats had readers only and lived off demo seeds).
        # One Welford sample per run for tokens_out and cost; anomalous runs
        # are folded in too (rolling baselines self-correct, and excluding
        # "anomalies" from the baseline would bake in survivor bias).
        # iterations baselines remain externally seeded: per-iteration counts
        # live in the loop detector, not per run.
        for r in bundle.runs:
            if r.agent_name is None:
                continue
            if r.tokens_out is None and r.cost_usd is None:
                continue
            await self._repo.upsert_agent_stats(
                r.agent_name,
                bundle.graph_type or "",
                tokens_out=float(r.tokens_out) if r.tokens_out is not None else None,
                cost=float(r.cost_usd) if r.cost_usd is not None else None,
                iterations=None,
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
