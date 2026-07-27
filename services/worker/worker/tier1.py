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
    ARTIFACT_OPAQUE,
    ARTIFACT_PARTIAL,
    classify_artifact_visibility,
    evaluate_schema,
    is_degenerate_output,
    load_prompt,
    render_prompt,
    truncate_for_judge,
)
from .signals import artifact_integrity_signals, check_rules_fingerprint
from .narrative import render_opaque_deliverable_reason, render_uninspected_media_caveat
from .store import ObjectStore, resolve_payload
from .streams import StreamConsumer, StreamPublisher, reclaim_pending_messages
from .types import (
    FLAG_ARTIFACT_INTEGRITY,
    FLAG_COST_OVERRUN,
    FLAG_DEGENERATE_OUTPUT,
    FLAG_FAILED_RUNS,
    FLAG_LOOP_ANOMALY,
    FLAG_REQUIRED_SECTION,
    FLAG_SCHEMA_VIOLATION,
    FLAG_TERMINAL_FORM,
    FLAG_UNINSPECTED_MEDIA,
    GROUP_TIER1,
    STREAM_GRAPHS_COMPLETED,
    STREAM_GRAPHS_TIER2,
    GraphBundle,
    RunRecord,
    Tier1Verdict,
)

logger = logging.getLogger(__name__)

# Ceiling for a terminal verdict reached on a deliverable whose media was never
# opened. Not a penalty — the text really was read — but 1.0 would assert the
# whole artifact was verified, and part of it was never seen. Mirrors the
# engine's convention of capping an attribution whose basis is unobserved.
_PARTIAL_VERIFICATION_CAP = 0.85


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
    ) -> tuple[str, float | None, str | None, dict | None, bool]:
        """Return (verdict, score, reasoning, form, partial).

        ``verdict`` in {ok, bad, not_checkable, error} and is the CONTENT
        dimension of the split rubric (substance vs goal); ``form`` is the FORM
        dimension dict ({"verdict", "requirement", "observed", "reasoning"}) or
        None when the judge produced none / the deliverable was not visible.
        ``partial`` is True when the verdict was reached on the deliverable's
        TEXT while part of what it delivers stayed unopened (images) — the
        caller records that limit as a soft flag.

        ``terminal_output`` must be the DELIVERABLE (see ``deliverable_run``), not
        the orchestrator root's empty output or a verifier's PASS/FAIL verdict.

        Honesty guard: if the deliverable content is not actually visible — the
        payload is empty, or it only *references* an artifact (a docx/pdf whose
        text was never embedded, an image that is merely named) — we cannot
        confirm OR deny the goal. Whatever the LLM guessed is overridden to
        ``not_checkable``. A confident "the final output is completely empty"
        over an unseen file is exactly the false-certainty this project exists to
        prevent.

        The guard is not all-or-nothing, because a multimodal deliverable is not
        an all-or-nothing object. A dossier whose own text IS present and which
        embeds its photographs inside itself is graded on the text it actually
        shows, and the unopened images ride out as a stated limit rather than as
        a discarded verdict (``ARTIFACT_PARTIAL``). Withholding the whole verdict
        there was false-uncertainty — the mirror image of the same sin.
        """
        # Deterministic, before trusting any LLM claim about content we may not
        # have shown it.
        if not (terminal_output or "").strip():
            return (
                "not_checkable",
                None,
                "deliverable payload was empty or absent",
                None,
                False,
            )
        visibility = classify_artifact_visibility(terminal_output)

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
            return "error", None, None, None, False
        # Rubric split: the judge answers {"content": {...}, "form": {...}}.
        # A legacy flat {"verdict", "score", "reasoning"} response (older
        # prompt, cassette replay) is accepted as content-only, form None.
        content = verdict.get("content")
        if not isinstance(content, dict):
            content = verdict
        raw_verdict = content.get("verdict")
        normalized = (
            "bad" if raw_verdict == "bad"
            else "ok" if raw_verdict == "ok"
            else "not_checkable" if raw_verdict == "not_checkable"
            else "error"
        )
        score = content.get("score")
        score_val = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        reasoning = content.get("reasoning")
        reasoning_val = reasoning if isinstance(reasoning, str) else None
        form_raw = verdict.get("form")
        form: dict | None = None
        if isinstance(form_raw, dict) and form_raw.get("verdict") in (
            "ok",
            "bad",
            "not_applicable",
        ):
            form = {
                "verdict": form_raw["verdict"],
                "requirement": form_raw.get("requirement")
                if isinstance(form_raw.get("requirement"), str)
                else None,
                "observed": form_raw.get("observed")
                if isinstance(form_raw.get("observed"), str)
                else None,
                "reasoning": form_raw.get("reasoning")
                if isinstance(form_raw.get("reasoning"), str)
                else None,
            }
        # Override: the artifact content was never in the payload, so a bad/ok
        # verdict is a guess about an unopened file. Force not_checkable — and
        # drop the form verdict with it (a form judged over an unseen file is
        # the same guess).
        if visibility.state == ARTIFACT_OPAQUE:
            return (
                "not_checkable",
                None,
                render_opaque_deliverable_reason(list(visibility.opaque_refs)),
                None,
                False,
            )
        # PARTIAL: the text was read, so the verdict stands — but it was reached
        # on the text alone. Saying so on the reasoning AND returning the partial
        # flag keeps "ok" from quietly meaning "the photographs were checked
        # too"; a verdict on a multimodal deliverable is never a full one.
        if visibility.state == ARTIFACT_PARTIAL:
            caveat = render_uninspected_media_caveat(list(visibility.uninspected_refs))
            reasoning_val = f"{reasoning_val} | {caveat}" if reasoning_val else caveat
            # ...and the SCORE has to say it too. No text rule separates "here is
            # the document, illustrated" from "here is my description of the file
            # I made" — 82 words of claims about an unopened logo satisfy every
            # structural test a dossier does. So a partial verdict never carries a
            # full-confidence pass: what was graded is the text, and the pictures
            # are unseen either way. Capping is the honest move that does not
            # require guessing which of the two we are looking at.
            if score_val is not None:
                score_val = min(score_val, _PARTIAL_VERIFICATION_CAP)
            return normalized, score_val, reasoning_val, form, True
        return normalized, score_val, reasoning_val, form, False

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

        terminal_form: dict | None = None
        # Whether the verdict below was reached on the deliverable's text while
        # part of what it delivers (images) stayed unopened. False by default:
        # the deterministic branch never issues a partial verdict, it issues a
        # reproduced fact.
        terminal_partial = False
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
            (
                verdict,
                score,
                reasoning,
                terminal_form,
                terminal_partial,
            ) = await self._terminal_judge(bundle, terminal_output)

        # A verdict reached on the text of a deliverable whose photographs were
        # never opened is a PARTIAL one, and it says so in the persisted flags —
        # not only in a sentence at the end of the reasoning, which nothing
        # downstream can key off. SOFT: an illustrated dossier is a normal
        # deliverable, not an incident, so it must not page tier2 on its own.
        if terminal_partial and FLAG_UNINSPECTED_MEDIA not in soft_flags:
            soft_flags.append(FLAG_UNINSPECTED_MEDIA)

        # Rubric split: a bad FORM verdict is a HARD flag of its own — the
        # deliverable visibly shipped in a form other than the explicitly
        # requested one. Without it a form-only miss (content ok) reached tier2
        # only via sampling.
        if terminal_form is not None and terminal_form.get("verdict") == "bad":
            flags.append(FLAG_TERMINAL_FORM)

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
                terminal_form=terminal_form,
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
        # Reclaim this group's own orphaned pending entries (a worker killed
        # mid-tier1 before XACK) and reprocess them alongside new messages, so a
        # graph never sits ingested-but-unanalysed waiting on a manual XADD.
        reclaimed = await reclaim_pending_messages(
            consumer,
            STREAM_GRAPHS_COMPLETED,
            GROUP_TIER1,
            settings.consumer_name,
            settings.reaper_idle_ms,
            settings.max_deliveries,
        )
        messages = await consumer.read(
            STREAM_GRAPHS_COMPLETED,
            GROUP_TIER1,
            settings.consumer_name,
            settings.stream_batch_size,
            settings.stream_block_ms,
        )
        for message in reclaimed + messages:
            graph_id = message.data.get("graph_id")
            try:
                if graph_id:
                    await processor.process(graph_id)
            except Exception:
                logger.exception("tier1: processing %s failed", graph_id)
                continue
            await consumer.ack(STREAM_GRAPHS_COMPLETED, GROUP_TIER1, message.id)
