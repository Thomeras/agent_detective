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

from blame_engine import (
    DIVERGENCE_KINDS,
    BlameReport,
    Finding,
    HarnessState,
    NodeScore,
    PROV_CONTRACT_PROPAGATION,
    PROV_REQUIRED_SECTION_CHECK,
    NoteRecord,
    RuleFingerprint,
    TerminalVerdict,
    derive_escalation,
    derive_incident,
    find_blame,
    has_note,
    is_verifier,
    serialize_note,
)

from .config import Settings
from .narrative import (
    HYPOTHESIS_LATER_PRODUCER,
    HYPOTHESIS_REPORTED,
    HYPOTHESIS_UNRESOLVED,
    STALE_CAUSE_PAYLOAD_DIVERGED,
    STALE_CAUSE_RULES_CHANGED,
    STALE_CAUSE_UNSTAMPED,
    render_breaker_reason,
    render_conformance,
    render_corrected_caveat,
    render_note,
    render_notes,
    render_hypothesis_basis,
    render_shipped_caveat,
    render_stale_cause,
    render_superseded_reason,
    signal,
)
from .graph_ops import build_blame_input, build_config, deliverable_run
from .judge_client import NullJudge, JudgeClient, judge_json_with_retries
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
    _unambiguous_contract_params,
    load_prompt,
    render_prompt,
    score_node,
    truncate_for_judge,
)
from .store import ObjectStore, resolve_payload
from .streams import StreamConsumer, StreamPublisher, reclaim_pending_messages
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

_WORD_RE = re.compile(r"\w+")


def classify_incident(
    report_type: str, flags: list[str], terminal_bad: bool
) -> tuple[str | None, str | None]:
    """Map a blame report + tier1 flags to an ``(incident_key, trigger)``.

    Thin delegate to the engine's ``derive_incident`` — the single home of the
    mapping (verdict refactor §2.3). Kept as a worker-local name so existing
    call sites and tests are unchanged; the logic (and its fixture-lock) lives
    in ``blame_engine.derive``.
    """
    return derive_incident(report_type, flags, terminal_bad)


# Verifier/gate node whose job is to PASS/FAIL work — scored on verdict
# correctness (role-aware), not on the reviewed artifact's quality. One home:
# blame_engine.roles (this was the third independent literal copy).
_is_verifier = is_verifier


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


def required_section_findings(
    check_rules: list,
    bundle: GraphBundle,
    payloads: dict[UUID, tuple[str | None, str | None]],
    deliverable_output: str | None,
) -> list[Finding]:
    """§2.4 required-section facts, one Finding per checkable REPRESENTATION.

    Each registered required_section is measured in TWO representations: the
    producers' span payloads and the shipped DELIVERABLE. The engine cannot see
    this (it holds no payloads); the worker does. The side findings go into
    ``find_blame(extra_findings=...)`` so the ENGINE's mandatory reconcile pass
    emits any divergence with refs into the report's real findings[] — the old
    worker-local reconcile shipped a divergence whose finding_refs indexed a
    throwaway list (they pointed at unrelated engine findings) and whose
    "present in deliverable" side existed nowhere in the report.
    """
    rules = [r for r in (check_rules or []) if r.kind == "required_section"]
    if not rules:
        return []
    producers = [
        r
        for r in bundle.runs
        if not _is_verifier(r.agent_name)
        and (r.output_inline or r.output_overflow_ref)
    ]
    findings: list[Finding] = []
    for rule in rules:
        if rule.graph_type not in (None, bundle.graph_type):
            continue
        spec = rule.spec
        name = spec.get("name") or spec.get("pattern")
        fact_key = f"required_section:{name}"
        # Producer-payload representation: present if ANY producer's payload has
        # the section (a checkable measurement; unknown payloads are skipped).
        producer_present: bool | None = None
        for run in producers:
            if rule.agent_name not in (None, run.agent_name):
                continue
            _in_text, out_text = payloads.get(run.run_id, (None, None))
            if out_text is None:
                continue
            present = section_present(out_text, spec)
            if present is None:
                continue
            producer_present = bool(producer_present) or present
        # Deliverable representation: present in the shipped artifact?
        deliverable_present = (
            section_present(deliverable_output, spec)
            if deliverable_output is not None
            else None
        )
        for subject, value in (
            ("producers", producer_present),
            ("deliverable", deliverable_present),
        ):
            if value is None:
                continue  # not checkable in this representation — no claim
            findings.append(
                Finding(
                    kind="required_section",
                    channel="deterministic",
                    subject=subject,
                    data={"value": "present" if value else "absent", "section": name},
                    provenance=HarnessState(detail=PROV_REQUIRED_SECTION_CHECK),
                    certainty=1.0,
                    fact_key=fact_key,
                )
            )
    return findings


def contract_propagation_findings(
    contract_results: list[dict], deliverable_run_id: str | None
) -> list[Finding]:
    """Typed Findings for VERIFIED contract propagation (§2.1 breach_propagated /
    breach_corrected). Passed to ``find_blame(extra_findings=...)`` BEFORE defect
    emission so the contract defect can cite the evidence for its own "shipped"
    claim (and the engine can derive the escalation in the single pass). An
    'unverified' result emits nothing — absence is what keeps the defect's
    ``unverified_in_channel`` caveat honest."""
    findings: list[Finding] = []
    for r in contract_results:
        status = r.get("status")
        if status not in ("propagated", "corrected"):
            continue
        findings.append(
            Finding(
                kind="breach_propagated" if status == "propagated" else "breach_corrected",
                channel="deterministic",
                subject="terminal",
                data={
                    "key": r.get("key"),
                    "from": r.get("from"),
                    "to": r.get("to"),
                    "basis": r.get("basis"),
                    **(
                        {"deliverable_run_id": deliverable_run_id}
                        if deliverable_run_id
                        else {}
                    ),
                },
                provenance=RuleFingerprint(
                    rule=f"contract_propagation:{r.get('key')}",
                    detail=PROV_CONTRACT_PROPAGATION,
                ),
                certainty=1.0,
            )
        )
    return findings


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
    *,
    content_measured: bool = True,
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
    # Only the keys the deliverable declares UNAMBIGUOUSLY settle propagation. A
    # payload carrying `file_type` twice with different values does not observe
    # either outcome — that is "unverified", the status this function already has
    # for "nothing observable", and it must not be resolved by whichever value
    # the walk reached first.
    # Widened by the violations' own keys, mirroring contract_violations'
    # out_keys: a breach on a DECLARED key outside the built-in list must be
    # observable in the deliverable too, or propagation on declared contracts
    # could never verify.
    violation_keys = frozenset(
        str(v.get("key", "")).lower() for v in contract_violations if v.get("key")
    )
    params = (
        _unambiguous_contract_params(
            _collect_contract_params(structured, _CONTRACT_KEYS | violation_keys)
        )
        if structured
        else {}
    )
    paths = _path_values(structured) if structured else []

    results: list[dict] = []
    seen: set[tuple] = set()
    for violation in contract_violations:
        key = str(violation.get("key", "")).lower()
        from_val = violation.get("from")
        to_val = violation.get("to")
        status: str | None = None
        basis: str | None = None
        # The rendering payload: WHICH observation settled the status. The note
        # is derived from it (worker/narrative.py), never written here — a
        # status and a sentence that disagree is then unrepresentable.
        detail: dict | None = None

        if key in params:
            observed = params[key]
            if _norm(observed) == _norm(to_val):
                status, basis = "propagated", "contract param match"
                detail = {"basis_kind": "param"}
            elif _norm(observed) == _norm(from_val):
                status, basis = "corrected", "contract param match"
                detail = {"basis_kind": "param"}

        if detail is None and key in _FILE_FORMAT_KEYS:
            for path in paths:
                ext = _path_ext(path)
                if ext is None:
                    continue
                if ext == str(to_val).strip().lower():
                    status = "propagated"
                    basis = f"artifact path {path!r} ends '.{ext}'"
                    detail = {"basis_kind": "path", "path": path, "ext": ext}
                    break
                if ext == str(from_val).strip().lower():
                    status = "corrected"
                    basis = f"artifact path {path!r} ends '.{ext}'"
                    detail = {"basis_kind": "path", "path": path, "ext": ext}
                    break

        if detail is None:
            status, basis = "unverified", "nothing observable"
            detail = {"basis_kind": "none"}

        record = NoteRecord(
            "contract_propagation",
            {
                "key": key,
                "from": from_val,
                "to": to_val,
                "agent": deliverable_agent,
                "status": status,
                # Whether the CONTENT channel produced anything at all on this
                # run. A propagated breach used to be narrated as "the run is
                # recovered in content but shipped with a violated contract" —
                # true only when content was measured and came back clean. On a
                # --no-judge run nothing measured it, and asserting recovery
                # from silence is the one thing this product must never do.
                "content_measured": content_measured,
                **detail,
            },
        )
        # Dedup on the RECORD, not on its rendered sentence — deduping by prose
        # made the identity of a fact depend on its wording.
        fingerprint = tuple(sorted((k, repr(v)) for k, v in record.data.items()))
        if fingerprint not in seen:
            seen.add(fingerprint)
            results.append(
                {
                    "key": key,
                    "from": from_val,
                    "to": to_val,
                    "status": status,
                    "basis": basis,
                    "record": serialize_note(record),
                    "note": render_note(record),
                }
            )
    return results


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
) -> tuple[float, list[NoteRecord], list[dict]]:
    """Cross-check independent evidence streams; return (confidence, note
    records, hypotheses).

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
    notes: list[NoteRecord] = []
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
    # Typed read of the engine's rationale stream: the fabrication-cascade row is
    # a cut_point VARIANT, asked for by slug + variant. Grepping the rendered
    # sentence ("fabrication cascade" in n) made this branch a hostage of the
    # engine's wording — a reworded template would have silently disabled it.
    fabrication = has_note(
        report.evidence.note_records, "cut_point", variant="fabrication"
    )
    if (
        fabrication
        and fact_propagation
        and last_producer is not None
        and any(last_producer in (f.get("found_in") or []) for f in fact_propagation)
    ):
        tension = True
        notes.append(NoteRecord("evidence_tension"))

    divergence = "degenerate_output" in tier1_flags and any(
        _is_verifier(agent_names.get(r)) for r in report.evidence.judge_notes
    )
    if divergence:
        notes.append(NoteRecord("representation_divergence"))

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
        # basis_code is the fact; basis is its one rendering (§2.4).
        hypotheses = [
            {
                "origin": reported_origin,
                "agent": agent_names.get(reported_origin, "unknown"),
                "basis_code": HYPOTHESIS_REPORTED,
                "basis": render_hypothesis_basis(HYPOTHESIS_REPORTED),
                "weight": origin_weight,
            },
            {
                "origin": last_producer,
                "agent": agent_names.get(last_producer, "unknown"),
                "basis_code": HYPOTHESIS_LATER_PRODUCER,
                "signals": bases,
                "basis": render_hypothesis_basis(
                    HYPOTHESIS_LATER_PRODUCER, {"signals": bases}
                ),
                "weight": alt_weight,
            },
            {
                "origin": None,
                "agent": None,
                "basis_code": HYPOTHESIS_UNRESOLVED,
                "basis": render_hypothesis_basis(HYPOTHESIS_UNRESOLVED),
                "weight": unresolved,
            },
        ]
        confidence = origin_weight
        notes.append(
            NoteRecord(
                "competing_origins",
                {
                    "origin_agent": agent_names.get(reported_origin, reported_origin),
                    "origin_weight": origin_weight,
                    "alt_agent": agent_names.get(last_producer, last_producer),
                    "alt_weight": alt_weight,
                    "unresolved": unresolved,
                },
            )
        )

    return confidence, notes, hypotheses


def escalate_shipped_latent_defect(
    report_type: str, contract_results: list[dict]
) -> tuple[str, list[str]]:
    """degraded_recovered + a contract breach VERIFIED (status 'propagated') in
    the shipped deliverable escalates to shipped_with_latent_defect.

    Thin delegate to the engine's ``derive_escalation`` — the single home of the
    escalation rule (verdict refactor §2.3). Worker-local name kept so existing
    call sites and tests are unchanged; logic + fixture-lock live in
    ``blame_engine.derive``.
    """
    return derive_escalation(report_type, contract_results)


_NULL_JUDGE = NullJudge()


def _localises(score: NodeScore) -> bool:
    """The deterministic half already named this node as the ORIGIN.

    Not merely "found something wrong": a signal has to claim it observed the
    fault ORIGINATE here (`originates`), which is what earns the deterministic
    attribution headline. A signal that only says the output is bad — an
    injection signature may well have arrived from upstream — leaves the
    question of where it came from open, and that question is the judge's.
    """
    return any(
        s.get("severity") == "fail" and s.get("originates")
        for s in score.deterministic_signals
    )


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

        async def _one(run: RunRecord, judge=None) -> NodeScore:
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
                self._judge if judge is None else judge,
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

        if self._settings.judge_gate:
            # Deterministic half first — no model, so this pass is free. When it
            # already localised a defect whose ORIGIN it observed, the per-node
            # judged pass has nothing left to decide about where the fault came
            # from, and its N calls are pure cost and pure exposure to a channel
            # measured to disagree with itself between sessions.
            pre = await asyncio.gather(*(_one(r, _NULL_JUDGE) for r in bundle.runs))
            if any(_localises(ns) for ns in pre):
                return (
                    {str(r.run_id): ns for r, ns in zip(bundle.runs, pre)},
                    {r.run_id: payloads[r.run_id] for r in bundle.runs},
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
                                stale_cause_code = STALE_CAUSE_UNSTAMPED
                            elif tier1.check_rules_hash != current_fp:
                                stale_cause_code = STALE_CAUSE_RULES_CHANGED
                            else:
                                stale_cause_code = STALE_CAUSE_PAYLOAD_DIVERGED
                            stale_cause = render_stale_cause(
                                stale_cause_code,
                                {
                                    "stored": tier1.check_rules_hash,
                                    "current": current_fp,
                                },
                            )
                    # Rubric split: bad/score/reasoning are the CONTENT
                    # dimension; the FORM dimension rides alongside so a
                    # format-only miss can never flip terminal_bad.
                    _tf = tier1.terminal_form or {}
                    terminal_verdict = TerminalVerdict(
                        bad=tier1.terminal_judge_verdict == "bad",
                        score=tier1.terminal_judge_score,
                        reasoning=tier1.terminal_judge_reasoning,
                        checkable=not stale,
                        stale=stale,
                        form_bad=_tf.get("verdict") == "bad",
                        form_requirement=_tf.get("requirement"),
                        form_observed=_tf.get("observed"),
                        form_reasoning=_tf.get("reasoning"),
                    )
                elif tier1.terminal_judge_verdict == "not_checkable":
                    # The judge never saw the deliverable — record the verdict so
                    # the report can explain the gap, but mark it NOT trustworthy
                    # ground truth so it drives neither a culprit nor an incident.
                    # No form dimension either: tier1 already drops the form
                    # verdict when the deliverable is invisible (same guess).
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
            # Worker-side facts the engine cannot compute (it holds no payloads)
            # are emitted BEFORE derivation (§F2.2) so defects can cite them and
            # the engine's reconcile pass sees them: required-section
            # representations and the deterministic contract-propagation
            # verification (breach_propagated is the evidence behind a
            # shipped_with_latent_defect headline).
            deliverable = deliverable_run(bundle)
            deliverable_output = None
            if deliverable is not None:
                _d_in, deliverable_output = payloads.get(
                    deliverable.run_id, (None, None)
                )
            contract_results: list[dict] = []
            _violations = [
                {"key": k, "from": a, "to": b}
                for ns in scores.values()
                for (k, a, b) in (ns.contract_violations or ())
            ]
            if _violations:
                contract_results = contract_propagation_check(
                    _violations,
                    str(deliverable.run_id) if deliverable is not None else None,
                    (deliverable.agent_name or "unknown")
                    if deliverable is not None
                    else "unknown",
                    deliverable_output,
                    content_measured=any(
                        ns.score is not None for ns in scores.values()
                    ),
                )
            extra_findings = contract_propagation_findings(
                contract_results,
                str(deliverable.run_id) if deliverable is not None else None,
            ) + required_section_findings(
                check_rules, bundle, payloads, deliverable_output
            )
            report = find_blame(blame_input, extra_findings=extra_findings or None)

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
            # Deterministic contract-propagation verification already ran BEFORE
            # find_blame (its findings joined the engine derivation); the
            # structured results still feed the notes + evidence stream below.
            contract_notes = [r["note"] for r in contract_results]
            shipped = [r for r in contract_results if r["status"] == "propagated"]
            corrected = [r for r in contract_results if r["status"] == "corrected"]

            # Escalation: "recovered" and "the breach verifiably shipped" cannot
            # coexist — the terminal is ok only because its judge is blind to
            # carried contract parameters. The customer still got a
            # contract-nonconformant artifact, so this run is a silent failure
            # in production, not a near-miss. Decided by the deterministic
            # propagation check alone (see escalate_shipped_latent_defect).
            effective_report_type, escalation_notes = escalate_shipped_latent_defect(
                report.report_type, contract_results
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
                # §2.4: divergences were emitted by the ENGINE's reconcile pass
                # (the worker-side representations went in as extra_findings, so
                # the refs index the report's real findings[]). Mirror them into
                # the dedicated list the UI reads.
                divergences = [
                    f
                    for f in (evidence.get("findings") or [])
                    if f.get("kind") in DIVERGENCE_KINDS
                ]
                if divergences:
                    evidence["reconcile_divergences"] = divergences
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
                            stamped = signal(
                                "structured_field_drop", "warn",
                                "structured_field_drop",
                                claim=entry["claim"], checked=entry["checked"],
                            ) | {
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
                    list(evidence["notes"]) + render_notes(extra_notes)
                    + contract_notes + escalation_notes
                )
                # Typed originals alongside (§2.4): the engine's records plus the
                # worker's own, so a consumer never has to parse the sentences.
                evidence["note_records"] = (
                    list(evidence.get("note_records") or [])
                    + [serialize_note(n) for n in extra_notes]
                    + [r["record"] for r in contract_results]
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
                        _cause = NoteRecord(
                            "terminal_stale_cause", {"cause": stale_cause}
                        )
                        evidence["notes"] = list(evidence["notes"]) + [
                            render_note(_cause)
                        ]
                        evidence["note_records"] = list(
                            evidence.get("note_records") or []
                        ) + [serialize_note(_cause)]
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
                        tv_ev["contract_conformance"] = render_conformance(shipped)
                        # The content axis decides WHICH caveat is true — a
                        # template written for one axis must never be pasted
                        # onto the other, so the axis state picks it.
                        tv_ev["caveat"] = render_shipped_caveat(
                            shipped,
                            content=(
                                "stale"
                                if tv_ev.get("stale")
                                else "bad"
                                if terminal_bad
                                else "ok"
                            ),
                        )
                    elif corrected and not any(
                        r["status"] == "unverified" for r in contract_results
                    ):
                        tv_ev["contract_conformance"] = "restored downstream (verified)"
                        if not terminal_bad:
                            tv_ev["caveat"] = render_corrected_caveat()
                    elif contract_results:
                        tv_ev["contract_conformance"] = "unverified"
                # The engine escalated in its own single pass and wrote the
                # escalated candidacy there (candidacy verdict
                # ``origin_escalated``) — the worker no longer touches either.
                # What remains here is the AUDIT record: the near-miss framing
                # was REFUTED, not merely outranked, so it is marked superseded
                # with the same structured mechanism used against LLM judge
                # assessments (score_overrides). A tool that flags superseded
                # claims of its judges but not its own holds a double standard.
                if effective_report_type == "shipped_with_latent_defect" and shipped:
                    evidence["superseded_notes"] = [
                        {
                            "slug": "degraded_recovered",
                            "superseded_by": "escalation",
                            "reason": render_superseded_reason(shipped),
                        }
                    ]

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
                    reason = render_breaker_reason(
                        n, self._settings.breaker_open_incidents, incident_trigger
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
        # Reclaim orphaned pending entries (worker killed mid-tier2 before XACK)
        # and reprocess them with new messages; the tier2 job claim keeps replay
        # idempotent, so a stalled analysis resumes without a manual XADD.
        reclaimed = await reclaim_pending_messages(
            consumer,
            STREAM_GRAPHS_TIER2,
            GROUP_TIER2,
            settings.consumer_name,
            settings.reaper_idle_ms,
            settings.max_deliveries,
        )
        messages = await consumer.read(
            STREAM_GRAPHS_TIER2,
            GROUP_TIER2,
            settings.consumer_name,
            settings.stream_batch_size,
            settings.stream_block_ms,
        )
        for message in reclaimed + messages:
            parsed = parse_tier2_message(message.data)
            try:
                if parsed is not None:
                    await processor.process(parsed)
            except Exception:
                # Job already marked failed; ack so it does not hot-loop, the
                # DLQ reaper handles persistent poison messages.
                logger.exception("tier2: message %s failed", message.id)
            await consumer.ack(STREAM_GRAPHS_TIER2, GROUP_TIER2, message.id)
