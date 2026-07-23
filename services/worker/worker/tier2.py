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
import unicodedata
from dataclasses import asdict
from uuid import UUID

from blame_engine import BlameReport, NodeScore, TerminalVerdict, find_blame

from .config import Settings
from .graph_ops import build_blame_input, build_config, deliverable_run
from .judge_client import JudgeClient, judge_json_with_retries
from .policy import (
    STREAM_CONTROL_SIGNALS,
    evaluate_policies,
    judge_prompts_fingerprint,
)
from .repository import Repo
from .checks_content import required_section_signals, section_present
from .signals import artifact_integrity_signals, check_rules_fingerprint
from .scoring import (
    _CONTRACT_KEYS,
    _collect_contract_params,
    _norm,
    _try_parse_json,
    load_prompt,
    render_prompt,
    score_node,
    truncate_for_judge,
)
from .store import ObjectStore, resolve_payload
from .streams import StreamConsumer, StreamPublisher
from .types import (
    FLAG_ARTIFACT_INTEGRITY,
    FLAG_REQUIRED_SECTION,
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
    "verification_gap",
    "degraded_recovered",
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
    if report_type == "shipped_with_latent_defect":
        # A VERIFIED contract breach in the shipped deliverable: a silent failure
        # reached production behind an ok terminal. Its own high-severity trigger
        # — alerting must be able to tell it apart from ordinary degraded quality
        # (and from the low-priority degraded_recovered near-miss).
        return "latent_defect", "latent_defect"
    if report_type in _QUALITY_REPORTS:
        return "degraded_quality", "degraded_quality"
    if "failed_runs" in flags:
        return "terminal_failure", "terminal_failure"
    if "cost_overrun" in flags:
        return "cost_overrun", "cost_overrun"
    if terminal_bad:
        return "terminal_failure", "terminal_failure"
    return None, None


_VERIFIER_HINTS = ("qa", "eval", "review", "verif", "validat", "check", "critic", "audit", "gate")


def _is_verifier(name: str | None) -> bool:
    """Verifier/gate node whose job is to PASS/FAIL work — scored on verdict
    correctness (role-aware), not on the reviewed artifact's quality."""
    n = (name or "").lower()
    return any(h in n for h in _VERIFIER_HINTS)


def _norm_text(value: str) -> str:
    """Unicode-normalized (NFC), casefolded text for diacritic-tolerant matching."""
    return unicodedata.normalize("NFC", value).casefold()


def _claim_matches(claim: str, text: str) -> bool:
    """Substring match, falling back to majority word overlap.

    Both sides are unicode-normalized and casefolded so Czech diacritics (and NFC
    vs NFD encodings of the same string) compare equal rather than silently
    missing — a false "not found" here understates real fact propagation.
    """
    c = _norm_text(claim).strip()
    if not c:
        return False
    t = _norm_text(text)
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


# File-format contract keys for which an artifact PATH's extension is admissible
# evidence of the shipped format (a path is structured data; prose is not).
_FILE_FORMAT_KEYS = frozenset({"file_type", "filetype", "format", "target_format"})


def _path_values(obj: object) -> list[str]:
    """String values under any key whose name contains "path" (recursive walk,
    same spirit as scoring._collect_contract_params). Structured artifact
    locations only — deliberately NOT a free-text scan of the payload, because
    prose mentioning ".docx" would false-positive."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and "path" in k.lower() and isinstance(v, str):
                    found.append(v)
                walk(v)
        elif isinstance(node, list):
            for el in node:
                walk(el)

    walk(obj)
    return found


def _path_ext(path: str) -> str | None:
    """Lowercased file extension of a path, without the dot; None when absent."""
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].strip().lower() or None


def contract_propagation_check(
    contract_violations: list[dict],
    deliverable_run_id: str | None,
    deliverable_agent: str,
    deliverable_output_text: str | None,
) -> list[dict]:
    """Deterministic check: did each contract breach PROPAGATE into the final
    deliverable?

    The blame engine sees no payloads, so its report can only say propagation is
    unverified. Tier2 DOES hold the deliverable producer's output — inspect it
    and say what was actually observed, per violation ``{key, from, to}``:

    - the payload's contract param equals ``to``   -> "propagated" (verified)
    - the payload's contract param equals ``from`` -> "corrected" (verified)
    - param absent but a structured artifact PATH's extension matches ``to`` /
      ``from`` (file-format keys only)             -> "propagated" / "corrected"
    - nothing observable (no payload, unparseable, no param, no path match)
                                                   -> "unverified"

    Returns one STRUCTURED result per violation (deduplicated):
    ``{key, from, to, status, basis, note}`` — the status drives the verdict
    escalation (a verified shipped breach is not a near-miss), so it must be a
    field, never re-parsed out of the prose note.
    """
    parsed = _try_parse_json(deliverable_output_text) if deliverable_run_id else None
    structured = parsed if isinstance(parsed, (dict, list)) else None
    params = _collect_contract_params(structured, _CONTRACT_KEYS) if structured else {}
    paths = _path_values(structured) if structured else []

    results: list[dict] = []
    seen_notes: set[str] = set()
    for violation in contract_violations:
        key = str(violation.get("key", "")).lower()
        from_val = violation.get("from")
        to_val = violation.get("to")
        status: str | None = None
        basis: str | None = None
        note: str | None = None

        if key in params:
            observed = params[key]
            if _norm(observed) == _norm(to_val):
                status, basis = "propagated", "contract param match"
                note = (
                    f"contract_propagation: rewritten {key}={to_val!r} observed in "
                    f"the deliverable producer '{deliverable_agent}' payload "
                    f"(basis: contract param match) — the breach PROPAGATED into "
                    f"the shipped artifact (verified); the run is recovered in "
                    f"content but shipped with a violated contract (latent defect)."
                )
            elif _norm(observed) == _norm(from_val):
                status, basis = "corrected", "contract param match"
                note = (
                    f"contract_propagation: the deliverable producer "
                    f"'{deliverable_agent}' payload carries the ORIGINAL "
                    f"{key}={from_val!r} (basis: contract param match) — the "
                    f"breach was corrected downstream; contract restored (verified)."
                )

        if note is None and key in _FILE_FORMAT_KEYS:
            for path in paths:
                ext = _path_ext(path)
                if ext is None:
                    continue
                if ext == str(to_val).strip().lower():
                    status = "propagated"
                    basis = f"artifact path {path!r} ends '.{ext}'"
                    note = (
                        f"contract_propagation: rewritten {key}={to_val!r} "
                        f"observed in the deliverable producer "
                        f"'{deliverable_agent}' payload (basis: artifact path "
                        f"{path!r} ends '.{ext}') — the breach PROPAGATED into "
                        f"the shipped artifact (verified); the run is recovered "
                        f"in content but shipped with a violated contract "
                        f"(latent defect)."
                    )
                    break
                if ext == str(from_val).strip().lower():
                    status = "corrected"
                    basis = f"artifact path {path!r} ends '.{ext}'"
                    note = (
                        f"contract_propagation: the deliverable producer "
                        f"'{deliverable_agent}' payload carries the ORIGINAL "
                        f"{key}={from_val!r} (basis: artifact path {path!r} ends "
                        f"'.{ext}') — the breach was corrected downstream; "
                        f"contract restored (verified)."
                    )
                    break

        if note is None:
            status, basis = "unverified", "nothing observable"
            note = (
                f"contract_propagation: the rewritten {key} is not observable in "
                f"the deliverable payload (basis: nothing observable — no "
                f"matching contract param or artifact path) — propagation "
                f"UNVERIFIED; verify the final artifact's format/contract out of "
                f"band."
            )
        if note not in seen_notes:
            seen_notes.add(note)
            results.append(
                {
                    "key": key,
                    "from": from_val,
                    "to": to_val,
                    "status": status,
                    "basis": basis,
                    "note": note,
                }
            )
    return results


def contract_propagation_notes(
    contract_violations: list[dict],
    deliverable_run_id: str | None,
    deliverable_agent: str,
    deliverable_output_text: str | None,
) -> list[str]:
    """Prose notes for the propagation check (see contract_propagation_check)."""
    return [
        r["note"]
        for r in contract_propagation_check(
            contract_violations,
            deliverable_run_id,
            deliverable_agent,
            deliverable_output_text,
        )
    ]


# When the independent evidence streams disagree on WHERE the fault started, the
# reported origin (what the score gap / content flag localised) stays the
# dominant hypothesis but can no longer own the whole confidence while a
# competing later-origin hypothesis is live. The localised confidence is divided
# between the two origins in this ratio (dominant : alternative), and the
# headline confidence drops to the dominant share. A single number sitting on top
# of two unresolved origins is exactly the false-certainty this product exists to
# expose — so we split it deterministically rather than papering over it.
_ORIGIN_DOMINANT_FRACTION = 0.6


def reconcile_evidence(
    report: BlameReport,
    fact_propagation: list[dict] | None,
    agent_names: dict[str, str],
    tier1_flags: list[str],
) -> tuple[float, list[str], list[dict]]:
    """Cross-check independent evidence streams; return (confidence, notes,
    hypotheses).

    A report whose evidence streams contradict each other cannot keep the
    confidence either stream would support alone, and — worse — must not present
    a single origin as settled while a competing origin is still live:

    - **evidence_tension** — the verdict says required content went missing at
      the origin, yet the origin's claims were found in the LAST producer's
      payload. Both cannot be fully right; the content may have survived the
      origin and been lost later.
    - **representation_divergence** — the terminal output is empty/degenerate
      while verifiers demonstrably reviewed a non-empty artifact: they saw a
      different representation than the terminal judge, which usually means the
      content was lost between the last producer and the terminal output.

    Either signal raises a *competing* origin (the render/export step, later than
    reported) the engine has not ruled out. When that happens we do NOT keep the
    flat single-origin confidence: we build an explicit competing-hypotheses
    breakdown (``hypotheses``) whose weights sum to 1.0 with an "unresolved"
    remainder, and lower the headline confidence to the dominant hypothesis's
    share. Reporting one origin at full confidence over two live hypotheses is
    the anti-pattern the divergence guard is here to prevent.
    """
    notes: list[str] = []
    hypotheses: list[dict] = []
    confidence = report.confidence

    reported_origin = report.culprit_run_ids[0] if report.culprit_run_ids else None
    producers = [
        r for r in report.evidence.topo_order if not _is_verifier(agent_names.get(r))
    ]
    # The render/export step: the LAST producer before the terminal output, which
    # is where both divergence signals suspect the content was actually lost.
    last_producer = producers[-1] if producers else None

    tension = False
    fabrication = any("fabrication cascade" in n for n in report.evidence.notes)
    if (
        fabrication
        and fact_propagation
        and last_producer is not None
        and any(last_producer in (f.get("found_in") or []) for f in fact_propagation)
    ):
        tension = True
        notes.append(
            "evidence_tension: claims from the origin were found in the final "
            "producer's payload although the verdict says required content went "
            "missing — the evidence streams disagree on where the content was "
            "lost, so the origin is not settled"
        )

    divergence = "degenerate_output" in tier1_flags and any(
        _is_verifier(agent_names.get(r)) for r in report.evidence.judge_notes
    )
    if divergence:
        notes.append(
            "representation_divergence: the terminal output is empty/degenerate "
            "while verifiers reviewed a non-empty artifact — they saw a "
            "different representation than the terminal judge; the content was "
            "likely lost between the last producer and the terminal output "
            "(check the render/export step), and the true origin may be later "
            "than reported"
        )

    # Both signals nominate a LATER producer (the render/export step) as an
    # alternative origin the engine has not disproved. Only build the breakdown
    # when that alternative is genuinely distinct from the reported origin — if
    # the reported origin already IS the last producer there is no "later" rival
    # to split against, and the flat confidence stands.
    if (
        (tension or divergence)
        and reported_origin is not None
        and last_producer is not None
        and last_producer != reported_origin
    ):
        origin_weight = round(confidence * _ORIGIN_DOMINANT_FRACTION, 4)
        alt_weight = round(confidence - origin_weight, 4)
        unresolved = round(1.0 - origin_weight - alt_weight, 4)
        bases = [
            b
            for b, present in (("evidence_tension", tension),
                               ("representation_divergence", divergence))
            if present
        ]
        hypotheses = [
            {
                "origin": reported_origin,
                "agent": agent_names.get(reported_origin, "unknown"),
                "basis": "reported origin — where the score gap / content flag "
                "localised the fault",
                "weight": origin_weight,
            },
            {
                "origin": last_producer,
                "agent": agent_names.get(last_producer, "unknown"),
                "basis": "later origin (" + " + ".join(bases) + ") — the same "
                "content survived to this producer, so the loss may be at the "
                "render/export step, later than reported",
                "weight": alt_weight,
            },
            {
                "origin": None,
                "agent": None,
                "basis": "unresolved — the evidence streams do not localise a "
                "single origin",
                "weight": unresolved,
            },
        ]
        confidence = origin_weight
        notes.append(
            "competing_origins: the origin is NOT settled — confidence is split "
            f"across '{agent_names.get(reported_origin, reported_origin)}' "
            f"(weight {origin_weight}) and the later producer "
            f"'{agent_names.get(last_producer, last_producer)}' "
            f"(weight {alt_weight}), with {unresolved} unresolved; the headline "
            f"confidence is lowered to the dominant hypothesis's share "
            f"({confidence}) rather than asserting one origin over two live "
            "hypotheses"
        )

    return confidence, notes, hypotheses


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
        # Role-aware judging: verifier/gate nodes are scored on the correctness of
        # their PASS/FAIL verdict, not on the artifact quality — otherwise a
        # rubber-stamp reads as "healthy" and the engine rewards the liar.
        self._verifier_prompt = load_prompt("judge_verifier.md")
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
        self, bundle: GraphBundle, baselines, contracts, semaphore, check_rules=None
    ) -> tuple[dict[str, NodeScore], dict[UUID, tuple[str | None, str | None]]]:
        payloads = {r.run_id: await self._payloads(r) for r in bundle.runs}
        # Unscoped required-section rules bind to the DELIVERABLE producer only
        # (document-level requirements; see score_node).
        _deliverable = deliverable_run(bundle)
        _deliverable_id = _deliverable.run_id if _deliverable is not None else None

        async def _one(run: RunRecord) -> NodeScore:
            input_text, output_text = payloads[run.run_id]
            template = (
                self._verifier_prompt if _is_verifier(run.agent_name) else self._judge_prompt
            )
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
                template,
                error_span_ids=[] if run.status != "failed" else ["failed"],
                min_artifact_bytes=self._settings.min_artifact_bytes,
                artifact_meta=run.artifact_meta,
                check_rules=check_rules,
                graph_type=bundle.graph_type,
                is_deliverable_producer=(run.run_id == _deliverable_id),
            )

        results = await asyncio.gather(*(_one(r) for r in bundle.runs))
        scores = {str(r.run_id): ns for r, ns in zip(bundle.runs, results)}
        # Return full (input, output) payloads: fact propagation must inspect a
        # successor's *input* (proof the claim reached it), not only its output.
        return scores, payloads

    @staticmethod
    def _fact_source(report: BlameReport, agent_names: dict[str, str]) -> str | None:
        """Content-bearing node whose output the claims run over.

        Claims must come from the DELIVERABLE flowing between producers. A
        verifier culprit (verification_gap) outputs a QA report — extracting
        "37 rules passed" from it is meta-noise, anti-evidence. Prefer the first
        non-verifier culprit, then the manifestation producer; a graph with only
        verifier suspects has no content source and gets no fact propagation.
        """
        for rid in report.culprit_run_ids:
            if not _is_verifier(agent_names.get(rid)):
                return rid
        for rid in report.evidence.manifestation_run_ids:
            if not _is_verifier(agent_names.get(rid)):
                return rid
        return None

    @staticmethod
    def _structured_claims(culprit_output: str) -> list[str] | None:
        """Scalar leaves of a JSON output — the deterministic claim source.

        Numbers and short strings that entered downstream nodes must come out
        consistently; extracting them is a parse, not an LLM opinion. Returns
        None when the output is not structured JSON (the LLM claims pass stays
        as the fallback for prose-only payloads).
        """
        parsed = _try_parse_json(culprit_output)
        if not isinstance(parsed, (dict, list)):
            return None
        leaves: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for el in node:
                    walk(el)
            elif isinstance(node, bool) or node is None:
                return
            elif isinstance(node, (int, float)):
                leaves.append(f"{node:g}" if isinstance(node, float) else str(node))
            elif isinstance(node, str):
                s = node.strip()
                if 3 <= len(s) <= 80:
                    leaves.append(s)

        walk(parsed)
        # Deduplicate preserving order, then put the DISTINCTIVE values first:
        # numbers, multi-word phrases and long strings carry propagation
        # information; bare config-ish tokens ("proposal", "standard") are
        # mostly noise and go last. Cap keeps the evidence readable.
        unique = list(dict.fromkeys(leaves))
        distinctive = [
            v for v in unique
            if any(c.isdigit() for c in v) or " " in v or len(v) >= 12
        ]
        rest = [v for v in unique if v not in distinctive]
        return (distinctive + rest)[:6]

    @staticmethod
    def _required_facts(
        check_rules,
        bundle: GraphBundle,
        payloads: dict[UUID, tuple[str | None, str | None]],
    ) -> list[dict]:
        """Required-facts checklist: each registered required_section rule ×
        presence across PRODUCER payloads. The negative finding — a required
        element found NOWHERE — is the headline evidence, so these entries lead
        the fact-propagation section instead of hiding behind claim noise."""
        rules = [r for r in (check_rules or []) if r.kind == "required_section"]
        if not rules:
            return []
        producers = [
            r
            for r in bundle.runs
            if not _is_verifier(r.agent_name)
            and (r.output_inline or r.output_overflow_ref)
        ]
        entries: list[dict] = []
        for rule in rules:
            if rule.graph_type not in (None, bundle.graph_type):
                continue
            spec = rule.spec
            name = spec.get("name") or spec.get("pattern")
            found_in: list[str] = []
            not_checkable: list[str] = []
            checked = 0
            for run in producers:
                if rule.agent_name not in (None, run.agent_name):
                    continue
                _in_text, out_text = payloads.get(run.run_id, (None, None))
                if out_text is None:
                    not_checkable.append(str(run.run_id))
                    continue
                present = section_present(out_text, spec)
                if present is None:
                    continue  # malformed rule: no claim either way
                checked += 1
                if present:
                    found_in.append(str(run.run_id))
            entries.append(
                {
                    "claim": f"required: {name} (pattern {spec.get('pattern')!r})",
                    "found_in": found_in,
                    "not_checkable": not_checkable,
                    "checked": checked,
                    "source": "required",
                }
            )
        return entries

    async def _fact_propagation(
        self,
        report: BlameReport,
        payloads: dict[UUID, tuple[str | None, str | None]],
        agent_names: dict[str, str],
    ) -> list[dict] | None:
        culprit = self._fact_source(report, agent_names)
        if culprit is None:
            return None
        _culprit_in, culprit_output = payloads.get(UUID(culprit), (None, None))
        if not culprit_output:
            return None
        # PRIMARY: deterministic structured-field extraction (a parse, with
        # exact normalized matching downstream). The LLM claims prompt runs
        # ONLY for prose outputs no parser can decompose.
        source = "structured"
        claims = self._structured_claims(culprit_output)
        if claims is None:
            source = "llm"
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
        # Downstream = everything after the source in topological order (the
        # propagation_path may start at a different culprit, e.g. a verifier).
        topo = report.evidence.topo_order or report.propagation_path
        downstream = (
            topo[topo.index(culprit) + 1 :]
            if culprit in topo
            else [rid for rid in report.propagation_path if rid != culprit]
        )
        # PRODUCERS only: a verifier's report MENTIONS facts it reviews ("the
        # artifact lacks the price breakdown" contains "price breakdown"), so a
        # substring hit in verifier commentary proves nothing about the fact
        # being PRESENT in the artifact — it can even prove its absence. Matching
        # commentary made fact propagation contradict the origin verdict.
        downstream = [rid for rid in downstream if not _is_verifier(agent_names.get(rid))]
        propagation: list[dict] = []
        for claim in claims:
            found_in: list[str] = []
            not_checkable: list[str] = []
            checked = 0
            for rid in downstream:
                node_in, node_out = payloads.get(UUID(rid), (None, None))
                # A successor's input carrying the claim proves the fact reached
                # it; its output carrying the claim proves it was forwarded.
                haystacks = [t for t in (node_in, node_out) if t]
                if not haystacks:
                    # No payload at all (e.g. the node failed) — we genuinely
                    # cannot tell, which is NOT the same as "not found".
                    not_checkable.append(rid)
                elif source == "structured":
                    # Structured values match EXACTLY (normalized substring) —
                    # no fuzzy word-overlap for a parsed field.
                    checked += 1
                    if any(_norm_text(claim) in _norm_text(t) for t in haystacks):
                        found_in.append(rid)
                else:
                    checked += 1
                    if any(_claim_matches(claim, t) for t in haystacks):
                        found_in.append(rid)
            propagation.append(
                {
                    "claim": claim,
                    "found_in": found_in,
                    "not_checkable": not_checkable,
                    # Distinguishes "absent from every checkable payload" from
                    # "there was nothing downstream to check".
                    "checked": checked,
                    "source": source,
                }
            )
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
            check_rules = await self._repo.read_check_rules()

            semaphore = asyncio.Semaphore(self._settings.judge_concurrency)
            scores, payloads = await self._score_graph(
                bundle, baselines, contracts, semaphore, check_rules
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
            stale_cause: str | None = None
            flags: list[str] = []
            if tier1 is not None:
                flags = list(tier1.flags)
                if tier1.terminal_judge_verdict in ("ok", "bad"):
                    # RECONCILIATION: a deterministically-decided bad verdict is
                    # only ground truth while its basis REPRODUCES. Tier1 ran at
                    # ingest time under the rule set of that moment; a
                    # re-analysis may run under different registered rules, or
                    # the payload/artifact may have diverged. Recompute the
                    # deterministic basis on the CURRENT rules/payload — if it
                    # no longer fails, the stored verdict is STALE and must not
                    # drive blame (the exact self-contradiction where the
                    # terminal claims a section missing while the propagation
                    # checklist shows it present).
                    stale = False
                    det_flags = {FLAG_ARTIFACT_INTEGRITY, FLAG_REQUIRED_SECTION} & set(
                        tier1.flags
                    )
                    if tier1.terminal_judge_verdict == "bad" and det_flags:
                        recon_deliverable = deliverable_run(bundle)
                        reproduced = False
                        if recon_deliverable is not None:
                            if FLAG_ARTIFACT_INTEGRITY in det_flags and any(
                                s["severity"] == "fail"
                                for s in artifact_integrity_signals(
                                    recon_deliverable.artifact_meta,
                                    min_bytes=self._settings.min_artifact_bytes,
                                )
                            ):
                                reproduced = True
                            if not reproduced and FLAG_REQUIRED_SECTION in det_flags:
                                _rin, rout = payloads.get(
                                    recon_deliverable.run_id, (None, None)
                                )
                                recon_rules = [
                                    r.spec
                                    for r in check_rules
                                    if r.kind == "required_section"
                                    and r.agent_name
                                    in (None, recon_deliverable.agent_name)
                                    and r.graph_type in (None, bundle.graph_type)
                                ]
                                if any(
                                    s["severity"] == "fail"
                                    for s in required_section_signals(
                                        rout, recon_rules
                                    )
                                ):
                                    reproduced = True
                        stale = not reproduced
                        if stale:
                            # WHY did the basis stop reproducing? The stored
                            # rule-set fingerprint answers with certainty.
                            current_fp = check_rules_fingerprint(
                                check_rules,
                                min_artifact_bytes=self._settings.min_artifact_bytes,
                            )
                            if tier1.check_rules_hash is None:
                                stale_cause = (
                                    "cause unknown — the tier1 verdict predates "
                                    "rule-set stamping (no fingerprint stored); "
                                    "cannot distinguish a rule change from "
                                    "artifact/payload divergence. Staleness is a "
                                    "property of the stored ANALYSIS, not a new "
                                    "fault of the agent's run"
                                )
                            elif tier1.check_rules_hash != current_fp:
                                stale_cause = (
                                    "the registered rule set CHANGED since tier1 "
                                    f"ran (fingerprint {tier1.check_rules_hash} -> "
                                    f"{current_fp}) — the old verdict was computed "
                                    "under different rules; not an artifact "
                                    "divergence. This is an ANALYSIS/rule-lifecycle "
                                    "matter on the operator side — the agent's run "
                                    "did not change and is not newly at fault"
                                )
                            else:
                                stale_cause = (
                                    "the rule set is UNCHANGED (fingerprint "
                                    f"{current_fp}) yet the payload no longer fails "
                                    "the check — the artifact/payload itself "
                                    "diverged between analysis passes "
                                    "(representation divergence); investigate the "
                                    "instrumentation/export path on the AGENT "
                                    "integration side"
                                )
                    terminal_verdict = TerminalVerdict(
                        bad=tier1.terminal_judge_verdict == "bad",
                        score=tier1.terminal_judge_score,
                        reasoning=tier1.terminal_judge_reasoning,
                        checkable=not stale,
                        stale=stale,
                    )
                elif tier1.terminal_judge_verdict == "not_checkable":
                    # The judge never saw the deliverable — record the verdict so
                    # the report can explain the gap, but mark it NOT trustworthy
                    # ground truth so it drives neither a culprit nor an incident.
                    terminal_verdict = TerminalVerdict(
                        bad=False,
                        score=tier1.terminal_judge_score,
                        reasoning=tier1.terminal_judge_reasoning,
                        checkable=False,
                    )

            config = build_config(
                threshold=self._settings.blame_threshold,
                gap_threshold=self._settings.gap_threshold,
                min_drop=self._settings.min_drop,
                max_loop_iterations=self._settings.max_loop_iterations,
                cum_drop_threshold=self._settings.cum_drop_threshold,
                cum_min_edges=self._settings.cum_min_edges,
                cum_step_min=self._settings.cum_step_min,
            )
            blame_input = build_blame_input(
                bundle, scores, terminal_verdict, baselines, config
            )
            report = find_blame(blame_input)

            agent_names = {str(r.run_id): (r.agent_name or "unknown") for r in bundle.runs}
            fact_propagation = await self._fact_propagation(report, payloads, agent_names)
            # Required-facts checklist LEADS the section: registered
            # requirements × presence across producers, negatives included —
            # "rozpočet → found in: none" is the headline evidence, not an
            # afterthought behind claim noise.
            required_entries = self._required_facts(check_rules, bundle, payloads)
            if required_entries:
                fact_propagation = required_entries + (fact_propagation or [])

            terminal_bad = (
                terminal_verdict is not None
                and terminal_verdict.bad
                and terminal_verdict.checkable
            )
            # Deterministic contract-propagation verification runs BEFORE
            # classification: a breach VERIFIED in the shipped deliverable must
            # escalate the verdict (and its alerting trigger), not just annotate
            # it. The blame engine has no payloads, but tier2 does — check the
            # deliverable producer's output and record what was actually observed.
            contract_results: list[dict] = []
            if report.evidence.contract_violations:
                deliverable = deliverable_run(bundle)
                deliverable_output = None
                if deliverable is not None:
                    _d_in, deliverable_output = payloads.get(
                        deliverable.run_id, (None, None)
                    )
                contract_results = contract_propagation_check(
                    list(report.evidence.contract_violations),
                    str(deliverable.run_id) if deliverable is not None else None,
                    (deliverable.agent_name or "unknown")
                    if deliverable is not None
                    else "unknown",
                    deliverable_output,
                )
            contract_notes = [r["note"] for r in contract_results]
            shipped = [r for r in contract_results if r["status"] == "propagated"]
            corrected = [r for r in contract_results if r["status"] == "corrected"]

            # Escalation: "recovered" and "the breach verifiably shipped" cannot
            # coexist — the terminal is ok only because its judge is blind to
            # carried contract parameters. The customer still got a
            # contract-nonconformant artifact, so this run is a silent failure
            # in production, not a near-miss.
            effective_report_type = report.report_type
            escalation_notes: list[str] = []
            if shipped and report.report_type == "degraded_recovered":
                effective_report_type = "shipped_with_latent_defect"
                detail = "; ".join(
                    f"{r['key']}: {r['from']!r}->{r['to']!r} ({r['basis']})"
                    for r in shipped
                )
                escalation_notes.append(
                    "escalation: verdict upgraded from degraded_recovered to "
                    f"shipped_with_latent_defect — the contract breach ({detail}) "
                    "was VERIFIED in the shipped artifact (see "
                    "contract_propagation). The pipeline recovered the CONTENT, "
                    "but a contract-nonconformant deliverable reached production "
                    "behind an ok terminal verdict: a silent failure, not a "
                    "near-miss"
                )

            incident_key, incident_trigger = classify_incident(
                effective_report_type, flags, terminal_bad
            )

            blame_draft = None
            if incident_key is not None:
                confidence, extra_notes, hypotheses = reconcile_evidence(
                    report, fact_propagation, agent_names, flags
                )
                evidence = serialize_evidence(report, fact_propagation)
                # Graph-level artifact integrity (docs/deterministic-signals.md):
                # the engine already assembled NODE-level signals from
                # NodeScore.deterministic_signals; recompute the same checks over
                # the graph's DELIVERABLE payload and append them with scope
                # "deliverable", deduped against the node-level entries (the
                # deliverable producer's own node signals are the same facts).
                deliverable = deliverable_run(bundle)
                if deliverable is not None:
                    node_level = evidence.get("deterministic_signals") or []
                    seen = {
                        (s.get("name"), s.get("run_id"), s.get("basis"))
                        for s in node_level
                    }
                    # Out-of-band attribute only — the payload is forgeable.
                    for sig in artifact_integrity_signals(
                        deliverable.artifact_meta,
                        min_bytes=self._settings.min_artifact_bytes,
                    ):
                        stamped = {
                            **sig,
                            "run_id": str(deliverable.run_id),
                            "agent": deliverable.agent_name or "unknown",
                            "provenance": "deterministic",
                            "scope": "deliverable",
                        }
                        key = (stamped["name"], stamped["run_id"], stamped["basis"])
                        if key not in seen:
                            seen.add(key)
                            node_level.append(stamped)
                    evidence["deterministic_signals"] = node_level

                # structured_field_drop: a parsed field from the culprit's
                # output absent from EVERY checkable downstream payload — the
                # deterministic propagation-loss signal (a parse and a diff,
                # no LLM). Entries with nothing checkable downstream are not
                # drops — "could not check" is never "dropped".
                if fact_propagation:
                    fact_src = self._fact_source(report, agent_names)
                    if fact_src is not None:
                        node_level = evidence.get("deterministic_signals") or []
                        seen = {
                            (s.get("name"), s.get("run_id"), s.get("basis"))
                            for s in node_level
                        }
                        for entry in fact_propagation:
                            if (
                                entry.get("source") != "structured"
                                or entry.get("found_in")
                                or not entry.get("checked")
                            ):
                                continue
                            stamped = {
                                "name": "structured_field_drop",
                                "severity": "warn",
                                "detail": (
                                    f"field value {entry['claim']!r} from the "
                                    "origin's output appears in no downstream "
                                    "payload"
                                ),
                                "basis": (
                                    f"exact normalized match over "
                                    f"{entry['checked']} checkable downstream "
                                    "payload(s)"
                                ),
                                "run_id": fact_src,
                                "agent": agent_names.get(fact_src, "unknown"),
                                "provenance": "deterministic",
                                "scope": "propagation",
                            }
                            key = (stamped["name"], stamped["run_id"], stamped["basis"] + entry["claim"])
                            if key not in seen:
                                seen.add(key)
                                node_level.append(stamped)
                        evidence["deterministic_signals"] = node_level
                evidence["notes"] = (
                    list(evidence["notes"]) + extra_notes + contract_notes
                    + escalation_notes
                )
                # The terminal section is the loudest element of the report.
                # It answers TWO independent questions — CONTENT (what the
                # verdict judged) and CONTRACT (format/params conformance) —
                # and a caveat template written for one scenario must never be
                # pasted onto the other ("ok in CONTENT only" above a bad
                # content verdict is a self-contradiction). Emit both axes as
                # structured fields; the caveat prose derives from the pair.
                if evidence.get("terminal_verdict") is not None:
                    tv_ev = evidence["terminal_verdict"]
                    # Reconciliation provenance: WHY a stale verdict stopped
                    # reproducing — rule change vs artifact divergence, settled
                    # by the stored rule-set fingerprint (migration 0008).
                    if tv_ev.get("stale") and stale_cause:
                        tv_ev["stale_cause"] = stale_cause
                        evidence["notes"] = list(evidence["notes"]) + [
                            f"terminal_stale_cause: {stale_cause}"
                        ]
                    # Provenance of the terminal decision: the deterministic
                    # deliverable checks (tier0) skip the LLM judge entirely.
                    tv_ev["decided_by"] = (
                        "deterministic"
                        if any(
                            f in flags
                            for f in (FLAG_ARTIFACT_INTEGRITY, FLAG_REQUIRED_SECTION)
                        )
                        else "llm_judge"
                    )
                    if shipped:
                        detail = "; ".join(
                            f"{r['key']} {r['to']!r} shipped, {r['from']!r} required"
                            for r in shipped
                        )
                        tv_ev["contract_conformance"] = (
                            f"nonconformant (verified): {detail}"
                        )
                        if tv_ev.get("stale"):
                            # The CONTENT axis is discarded (not reproducible)
                            # — it is neither ok nor bad. Only the contract
                            # axis stands; "ok in CONTENT only" here would
                            # assert a content verdict we just threw away.
                            tv_ev["caveat"] = (
                                f"content verdict is STALE (not reproducible — "
                                f"see the cause above); the contract axis "
                                f"stands on its own evidence: "
                                f"contract-nonconformant deliverable VERIFIED "
                                f"— {detail} (see contract_propagation)"
                            )
                        elif terminal_bad:
                            tv_ev["caveat"] = (
                                f"TWO independent faults: content is bad (see "
                                f"reasoning) AND the deliverable is "
                                f"contract-nonconformant — {detail} (verified, "
                                "see contract_propagation)"
                            )
                        else:
                            tv_ev["caveat"] = (
                                f"ok in CONTENT only — contract-nonconformant "
                                f"deliverable VERIFIED: {detail} (see "
                                "contract_propagation)"
                            )
                    elif corrected and not any(
                        r["status"] == "unverified" for r in contract_results
                    ):
                        tv_ev["contract_conformance"] = "restored downstream (verified)"
                        if not terminal_bad:
                            tv_ev["caveat"] = (
                                "ok — the mid-pipeline contract breach was "
                                "corrected downstream (verified); the deliverable "
                                "conforms to the carried contract"
                            )
                    elif contract_results:
                        tv_ev["contract_conformance"] = "unverified"
                # Escalation must rewrite the NARRATIVE, not just the verdict
                # type: the engine generated candidacy for degraded_recovered
                # ("a near-miss, not the origin of a live failure") BEFORE the
                # propagation check proved the breach shipped. Leaving that text
                # standing next to "silent defect shipped" is a literal
                # self-negation in one document. Rebuilt from data, not patched.
                if effective_report_type == "shipped_with_latent_defect" and shipped:
                    detail = "; ".join(
                        f"{r['key']}: {r['from']!r}->{r['to']!r} ({r['basis']})"
                        for r in shipped
                    )
                    # The engine's near-miss finding is REFUTED, not merely
                    # outranked — mark it superseded with the same structured
                    # mechanism used against LLM judge assessments
                    # (score_overrides), so the UI applies one visual language
                    # to both. A tool that flags superseded claims of its
                    # judges but not its own holds a double standard.
                    evidence["superseded_notes"] = [
                        {
                            "slug": "degraded_recovered",
                            "superseded_by": "escalation",
                            "reason": (
                                "the near-miss framing was refuted: the "
                                f"contract breach ({detail}) was VERIFIED in "
                                "the shipped artifact — verdict escalated to "
                                "shipped_with_latent_defect"
                            ),
                        }
                    ]
                    for _culprit_id in report.culprit_run_ids:
                        _cscore = (evidence.get("score_map") or {}).get(_culprit_id)
                        evidence.setdefault("candidacy", {})[_culprit_id] = (
                            f"origin (escalated) — degraded here "
                            + (f"(score {_cscore:.2f}) " if _cscore is not None else "")
                            + "and the contract breach was VERIFIED in the "
                            f"shipped artifact ({detail}). Content recovered "
                            "downstream and the terminal is ok on CONTENT, but a "
                            "contract-nonconformant deliverable reached "
                            "production — a silent failure, no longer a near-miss"
                        )

                # When the streams disagree on the origin the breakdown replaces
                # the engine's empty default (which asserts a settled origin).
                if hypotheses:
                    evidence["hypotheses"] = hypotheses
                blame_draft = BlameDraft(
                    report_type=effective_report_type,
                    culprit_run_ids=[UUID(r) for r in report.culprit_run_ids],
                    propagation_path=[UUID(r) for r in report.propagation_path],
                    confidence=confidence,
                    downstream_cost_usd=report.downstream_cost_usd,
                    unscored_run_ids=[UUID(r) for r in report.unscored_run_ids],
                    evidence=evidence,
                    # The worker's OWN judge-prompt fingerprint (migration
                    # 0009); the judge MODEL is not recorded — known limitation.
                    judge_prompt_hash=judge_prompts_fingerprint(),
                )

            # Shadow policy gates (roadmap 2.2), evaluated post-blame — a
            # documented deviation from the roadmap's tier1 sketch, because
            # only this point has flags + deterministic signals + the effective
            # report type + costs together. Decisions are RECORDS of what a
            # gate would have done; nothing here intercepts anything.
            if blame_draft is not None:
                signal_entries = blame_draft.evidence.get("deterministic_signals") or []
            else:
                # No report was drafted — fall back to the node-level signals
                # the scorer assembled (same facts, minus deliverable-scope
                # enrichment that only exists on a drafted report).
                signal_entries = [
                    s for ns in scores.values() for s in (ns.deterministic_signals or ())
                ]
            graph_cost = (
                bundle.total_cost_usd
                if bundle.total_cost_usd is not None
                else sum(r.cost_usd or 0.0 for r in bundle.runs)
            )
            scored = [
                row.quality_score
                for row in node_scores
                if row.quality_score is not None
            ]
            shadow_decisions = evaluate_policies(
                await self._repo.read_policy_rules(),
                flags=flags,
                signal_names=[
                    s.get("name")
                    for s in signal_entries
                    if isinstance(s, dict) and s.get("name")
                ],
                report_type=effective_report_type,
                graph_cost=graph_cost,
                min_node_score=min(scored) if scored else None,
            )
            if shadow_decisions:
                # Governance decisions live ONLY in the policy_decisions audit
                # table (and its own UI panel). They are a different category of
                # claim from the causal, evidence-backed findings in the
                # report's reasoning notes — mixing them dilutes both.
                await self._repo.insert_policy_decisions(graph_id, shadow_decisions)

            outcome = await self._repo.persist_tier2_result(
                dedup_key=message.dedup_key,
                node_scores=node_scores,
                graph_id=graph_id,
                incident_key=incident_key,
                incident_trigger=incident_trigger,
                blame=blame_draft,
                # A completed analysis is authoritative: stale incidents of this
                # graph under a DIFFERENT classification (or any, when this
                # analysis came back clean) are superseded, not left paging.
                # (The graph-not-found early return above does NOT supersede —
                # no analysis actually ran there.)
                supersede_others=True,
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

        # Circuit breaker v1 (roadmap 2.3), evaluated after a NEW incident is
        # committed: when one agent accumulates enough open incidents, RECORD
        # an open breaker and publish the control signal. Honest framing:
        # Agent Detective observes and cannot stop anything — this records a
        # decision; enforcement only happens if the integration (via the
        # opt-in detective_sdk poller) reads this state and honors it.
        if outcome.incident_id is not None and outcome.is_new and blame_draft is not None:
            culprit_agents = sorted(
                {
                    name
                    for rid in blame_draft.culprit_run_ids
                    if (name := agent_names.get(str(rid))) and name != "unknown"
                }
            )
            for name in culprit_agents:
                n = await self._repo.count_open_incidents_for_agent(name)
                if n >= self._settings.breaker_open_incidents:
                    reason = (
                        f"{n} open incidents (threshold "
                        f"{self._settings.breaker_open_incidents}); "
                        f"latest trigger {incident_trigger}"
                    )
                    await self._repo.upsert_breaker("agent_name", name, "open", reason)
                    await self._publisher.xadd_json(
                        STREAM_CONTROL_SIGNALS,
                        {
                            "schema_version": 1,
                            "scope_kind": "agent_name",
                            "scope_value": name,
                            "state": "open",
                            "reason": reason,
                        },
                    )
        logger.info(
            "tier2 graph=%s report=%s incident=%s is_new=%s",
            graph_id,
            effective_report_type,
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
