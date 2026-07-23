"""Per-node scoring (build spec section 4.3 step 2).

A node's quality score is a weighted mean over three independent components:

- **schema**: validate the output against a registered ``output_contract``
  (1.0 valid / 0.0 invalid); None when no contract is registered.
- **judge**: an LLM verdict on the output *relative to the node's input*
  (``task_score`` in 0..1); None after retries are exhausted.
- **heuristics**: cheap signals — empty/degenerate output, repetition ratio,
  error spans / failed status, retry count, token z-score vs ``agent_stats``.

There is deliberately **no** downstream-consistency component (spec section 2,
defect 4). The composite is ``Σ w_i·s_i / Σ w_i`` over the non-None components;
when the judge is None *and* the remaining weight is below ``SCORE_MIN_WEIGHT``
the node is left unscored (score None + ``unscored_reason``), so a missing judge
never silently presumes innocence.
"""

from __future__ import annotations

import asyncio
import math
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from blame_engine import NodeScore

from .behavioral import (
    duplicate_side_effect_signals,
    loop_fingerprint_signals,
    parse_tool_calls,
    retry_storm_signals,
    tool_args_signals,
)
from .checks_content import (
    language_mismatch_signals,
    required_section_signals,
    sum_invariant_signals,
    temporal_invariant_signals,
    unit_inconsistency_signals,
)
from .checks_security import injection_signature_signals, sensitive_data_signals
from .graph_ops import _VERIFIER_HINTS
from .judge_client import JudgeClient, judge_json_with_retries
from .signals import artifact_integrity_signals
from .types import AgentStat, CheckRule, OutputContract, RunRecord

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Deterministic judge truncation window (spec 4.3 step 2).
_JUDGE_HEAD_BYTES = 12 * 1024
_JUDGE_TAIL_BYTES = 4 * 1024

_WORD_RE = re.compile(r"\S+")

# Role hints for planning/orchestration nodes: their correct output is a plan
# or a routing decision, never the deliverable's content. The role is resolved
# deterministically HERE and stated verbatim in the judge prompt — inferring it
# from the agent name was left to the LLM, and role-blind verdicts kept making
# planners come out as false origins ("provides a plan but not the requested
# one-page overview" is a category error, not a finding).
_PLANNER_HINTS = (
    "think", "plan", "orchestrat", "rout", "coordinat", "supervis", "dispatch"
)


def node_role(agent_name: str | None, *, is_deliverable_producer: bool = False) -> str:
    """Deterministic role classification, stated to the judge as ground truth."""
    n = (agent_name or "").lower()
    if any(h in n for h in _PLANNER_HINTS):
        return (
            "PLANNER — its correct output is a plan/outline/routing decision; "
            "the deliverable's content is produced by later nodes"
        )
    if any(h in n for h in _VERIFIER_HINTS):
        return "VERIFIER — its correct output is a verdict on another node's work"
    if is_deliverable_producer:
        return (
            "DELIVERABLE PRODUCER — its output IS the artifact shipped to the "
            "user; deliverable-level requirements apply in full"
        )
    return (
        "INTERMEDIATE PRODUCER — its output feeds later steps; judge it "
        "against what its input asked THIS step to produce"
    )


# Input-contract preservation: parameters an agent must carry through unchanged
# unless explicitly told to change them. A silent rewrite (e.g. think flipping
# file_type docx->md) is a deterministic fault — detectable without any LLM — and
# is the archetypal "first node that broke quality" this product exists to catch.
_CONTRACT_KEYS = frozenset(
    {
        "file_type",
        "filetype",
        "format",
        "target_format",
        "doc_kind",
        "medium",
        "lang",
        "language",
        "locale",
    }
)
# A confirmed contract violation forces the node below any sane blame threshold,
# so it surfaces as a cut_point culprit rather than hiding behind a fluent judge.
_CONTRACT_VIOLATION_SCORE = 0.15

# ANY fail-severity deterministic signal (artifact integrity, missing required
# section, sum-invariant breach, language mismatch, duplicate side effect,
# invalid tool args, ...) is a harder fact than a rewritten parameter — the
# output is provably wrong — so the generalized ceiling sits below the contract
# override (0.15).
_DETERMINISTIC_FAIL_SCORE = 0.10

# Structured judge flags and the deterministic score ceiling each one enforces.
# The point is calibration the judge cannot wriggle out of: once its reasoning
# admits a shortcoming via a flag, the number cannot stay in the "good" band
# ("comprehensive proposal, 0.71" and "lacks requested details, 0.89" both
# violated that). Unknown flags pass through uncapped but are still recorded.
_JUDGE_FLAG_CAPS: dict[str, float] = {
    "missing_required_content": 0.55,
    "ignored_instruction": 0.55,
    "factual_error": 0.45,
    "unverifiable_artifact": 0.60,
}

# Output that references a binary/file artifact whose CONTENT is not present in
# the payload. A judge reading such a payload sees a claim ("wrote report.docx"),
# not the work — scores built on it are built on sand. Text formats that are
# usually echoed inline (md/html/txt/json) are deliberately excluded.
_OPAQUE_ARTIFACT_RE = re.compile(
    r"[\w./\\:-]+\.(?:docx|doc|pdf|pptx|ppt|xlsx|xls|zip|tar|gz|png|jpe?g|bin)\b",
    re.IGNORECASE,
)
# STRUCTURAL markers for embedded artifact content: the block form the
# instrumentation appends ("[artifact_text <path>]:") or an actual JSON field
# named artifact_text. A BARE substring is not accepted — prose that merely
# mentions the word 'artifact_text' (an agent quoting the convention, a QA note
# "artifact_text will be attached") must not self-exempt the payload from
# opacity detection.
_ARTIFACT_TEXT_MARKERS = ("[artifact_text ", '"artifact_text"')


def opaque_artifact_refs(output_text: str | None) -> list[str]:
    """File-artifact references in the output whose content is not embedded.

    Returns the matched references when the payload names a binary artifact but
    carries no extracted content (no structural ``artifact_text`` marker) — the
    signal that every judge downstream is grading a description, not the
    artifact.
    """
    if not output_text or any(m in output_text for m in _ARTIFACT_TEXT_MARKERS):
        return []
    return list(dict.fromkeys(m.group(0) for m in _OPAQUE_ARTIFACT_RE.finditer(output_text)))


def _norm(value: object) -> str:
    """Casefolded, unicode-normalized string for tolerant value comparison."""
    import unicodedata

    return unicodedata.normalize("NFC", str(value)).strip().casefold()


def _try_parse_json(text: str | None) -> object | None:
    """Parse JSON, tolerating prose wrapping a single {...} object."""
    if not text:
        return None
    import json as _json

    try:
        return _json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return _json.loads(match.group(0))
            except ValueError:
                return None
    return None


def _collect_contract_params(obj: object, keys: frozenset[str]) -> dict[str, object]:
    """Recursively collect the first scalar value seen for each contract key."""
    found: dict[str, object] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in keys and not isinstance(v, (dict, list)):
                    found.setdefault(k.lower(), v)
                walk(v)
        elif isinstance(node, list):
            for el in node:
                walk(el)

    walk(obj)
    return found


def contract_violations(
    input_text: str | None, output_text: str | None, keys: frozenset[str] = _CONTRACT_KEYS
) -> list[tuple[str, object, object]]:
    """Contract parameters present in BOTH input and output whose value changed.

    Returns ``(key, input_value, output_value)`` triples. Empty when input or
    output is not JSON, or when every shared contract parameter was preserved.
    """
    parsed_in = _try_parse_json(input_text)
    parsed_out = _try_parse_json(output_text)
    if not isinstance(parsed_in, (dict, list)) or not isinstance(parsed_out, (dict, list)):
        return []
    in_params = _collect_contract_params(parsed_in, keys)
    out_params = _collect_contract_params(parsed_out, keys)
    violations: list[tuple[str, object, object]] = []
    for key, in_val in in_params.items():
        if key in out_params and _norm(in_val) != _norm(out_params[key]):
            violations.append((key, in_val, out_params[key]))
    return violations


def load_prompt(name: str) -> str:
    """Load a prompt template from ``worker/prompts``."""
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def render_prompt(template: str, values: dict[str, str]) -> str:
    """Substitute ``<<KEY>>`` placeholders. Avoids ``str.format`` so prompt
    text may contain literal JSON braces without escaping."""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"<<{key}>>", value)
    return rendered


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def truncate_for_judge(
    text: str,
    head_bytes: int = _JUDGE_HEAD_BYTES,
    tail_bytes: int = _JUDGE_TAIL_BYTES,
) -> str:
    """Deterministically shrink a payload for the judge prompt.

    Keeps ``head_bytes`` from the start and ``tail_bytes`` from the end, joined
    by an explicit ``...[truncated N bytes]...`` marker. Short payloads pass
    through unchanged.
    """
    data = text.encode("utf-8")
    if len(data) <= head_bytes + tail_bytes:
        return text
    dropped = len(data) - head_bytes - tail_bytes
    head = data[:head_bytes].decode("utf-8", errors="ignore")
    tail = data[-tail_bytes:].decode("utf-8", errors="ignore")
    return f"{head}...[truncated {dropped} bytes]...{tail}"


def repetition_ratio(text: str) -> float:
    """Fraction of repeated whitespace tokens: ``1 - unique/total`` (0..1)."""
    words = _WORD_RE.findall(text)
    if len(words) < 4:
        return 0.0
    return 1.0 - len(set(words)) / len(words)


def is_degenerate_output(text: str | None) -> bool:
    """True for empty, whitespace-only or highly repetitive output."""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return repetition_ratio(stripped) >= 0.75


# --- JSON Schema (subset) validation -------------------------------------
# A dependency-free validator covering the keywords realistic output contracts
# use: type, required, properties, items, enum. Unknown keywords are ignored
# (lenient), so a contract never fails a node for a keyword we do not model.

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _matches_type(instance: Any, type_name: str) -> bool:
    if type_name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if type_name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if type_name == "boolean":
        return isinstance(instance, bool)
    expected = _JSON_TYPES.get(type_name)
    return expected is not None and isinstance(instance, expected)


def validate_json_schema(instance: Any, schema: dict[str, Any]) -> bool:
    """Validate ``instance`` against a JSON Schema subset. True when valid."""
    if not isinstance(schema, dict):
        return True
    type_kw = schema.get("type")
    if isinstance(type_kw, str) and not _matches_type(instance, type_kw):
        return False
    if isinstance(type_kw, list) and not any(_matches_type(instance, t) for t in type_kw):
        return False
    if "enum" in schema and instance not in schema["enum"]:
        return False
    if isinstance(instance, dict):
        for key in schema.get("required", []) or []:
            if key not in instance:
                return False
        for key, subschema in (schema.get("properties") or {}).items():
            if key in instance and not validate_json_schema(instance[key], subschema):
                return False
    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            if not all(validate_json_schema(el, items) for el in instance):
                return False
    return True


def _select_contract(
    contracts: list[OutputContract], agent_name: str | None, agent_version: str | None
) -> OutputContract | None:
    """First contract whose agent_name matches and version pattern fits."""
    if agent_name is None:
        return None
    for contract in contracts:
        if contract.agent_name != agent_name:
            continue
        pattern = contract.agent_version_pattern
        if pattern and agent_version is not None and not fnmatch(agent_version, pattern):
            continue
        return contract
    return None


def evaluate_schema(
    output_text: str | None,
    contracts: list[OutputContract],
    agent_name: str | None,
    agent_version: str | None,
) -> float | None:
    """Schema component: 1.0 valid / 0.0 invalid; None if no contract applies."""
    contract = _select_contract(contracts, agent_name, agent_version)
    if contract is None:
        return None
    if output_text is None:
        return 0.0
    import json

    try:
        instance = json.loads(output_text)
    except ValueError:
        return 0.0
    return 1.0 if validate_json_schema(instance, contract.json_schema) else 0.0


def evaluate_heuristics(
    output_text: str | None,
    run: RunRecord,
    baseline: AgentStat | None,
    *,
    error_span_ids: list[str],
    retry_count: int,
) -> float | None:
    """Heuristics component in 0..1; None when there is no output to inspect."""
    if output_text is None:
        return None
    stripped = output_text.strip()
    if not stripped:
        return 0.0
    score = 1.0
    rep = repetition_ratio(stripped)
    if rep > 0.5:
        score -= min(0.6, (rep - 0.5) * 2.0)
    if error_span_ids:
        score -= 0.3
    if run.status == "failed":
        score -= 0.5
    elif run.status == "degraded":
        score -= 0.2
    if retry_count > 0:
        score -= min(0.3, 0.1 * retry_count)
    if (
        baseline is not None
        and baseline.tokens_out_mean is not None
        and baseline.tokens_out_std
        and run.tokens_out is not None
    ):
        z = (run.tokens_out - baseline.tokens_out_mean) / baseline.tokens_out_std
        if abs(z) > 3.0:
            score -= 0.2
    return _clamp(score)


def composite_score(
    components: dict[str, float | None],
    weights: dict[str, float],
    min_weight: float,
) -> tuple[float | None, str | None]:
    """Weighted mean over non-None components with the judge-floor rule.

    Returns ``(score, unscored_reason)``. When the judge is None and the sum of
    the remaining present weights is below ``min_weight`` the node is unscored.
    """
    present = {k: v for k, v in components.items() if v is not None}
    if components.get("judge") is None:
        remaining = sum(weights.get(k, 0.0) for k in present)
        if remaining < min_weight:
            return None, "insufficient_components"
    total_weight = sum(weights.get(k, 0.0) for k in present)
    if not present or total_weight == 0.0:
        return None, "insufficient_components"
    value = sum(weights.get(k, 0.0) * v for k, v in present.items()) / total_weight
    return _clamp(value), None


async def score_node(
    run: RunRecord,
    input_text: str | None,
    output_text: str | None,
    contracts: list[OutputContract],
    baseline: AgentStat | None,
    judge: JudgeClient,
    semaphore: asyncio.Semaphore,
    weights: dict[str, float],
    min_weight: float,
    judge_prompt_template: str,
    *,
    error_span_ids: list[str] | None = None,
    retry_count: int = 0,
    min_artifact_bytes: int = 64,
    artifact_meta: str | None = None,
    check_rules: list[CheckRule] | None = None,
    graph_type: str | None = None,
    is_deliverable_producer: bool = False,
    judge_sleep: Any = asyncio.sleep,
) -> NodeScore:
    """Score one run into a ``blame_engine.NodeScore`` (run_id as a string)."""
    run_id = str(run.run_id)
    error_span_ids = error_span_ids or []

    if output_text is None:
        return NodeScore(
            run_id=run_id,
            score=None,
            components={"schema": None, "judge": None, "heuristics": None},
            input_flawed=None,
            unscored_reason="payload_missing",
            judge_note=None,
        )

    schema_component = evaluate_schema(
        output_text, contracts, run.agent_name, run.agent_version
    )
    heuristics_component = evaluate_heuristics(
        output_text, run, baseline, error_span_ids=error_span_ids, retry_count=retry_count
    )

    judge_component: float | None = None
    input_flawed: bool | None = None
    judge_note: str | None = None
    flags: list[str] = []
    prompt = render_prompt(
        judge_prompt_template,
        {
            "AGENT_NAME": run.agent_name or "unknown",
            "NODE_ROLE": node_role(
                run.agent_name, is_deliverable_producer=is_deliverable_producer
            ),
            "NODE_INPUT": truncate_for_judge(input_text or ""),
            "NODE_OUTPUT": truncate_for_judge(output_text),
        },
    )
    async with semaphore:
        verdict = await judge_json_with_retries(judge, prompt, sleep=judge_sleep)
    if verdict is not None:
        raw = verdict.get("task_score")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            judge_component = _clamp(float(raw))
        flawed = verdict.get("input_flawed")
        if isinstance(flawed, bool):
            input_flawed = flawed
        reasoning = verdict.get("reasoning")
        if isinstance(reasoning, str):
            judge_note = reasoning
        raw_flags = verdict.get("flags")
        if isinstance(raw_flags, list):
            flags = [f for f in raw_flags if isinstance(f, str) and f.strip()][:8]

    # Deterministic artifact-opacity check: the output claims a file artifact
    # but its content is not in the payload. No judge saw the actual work, so
    # "meets all requirements" would be an assertion about an unopened file.
    opaque_refs = opaque_artifact_refs(output_text)
    if opaque_refs and "unverifiable_artifact" not in flags:
        flags.append("unverifiable_artifact")
        opaque_note = (
            "artifact content not in payload (unverifiable): "
            + ", ".join(opaque_refs[:3])
        )
        judge_note = f"{judge_note} | {opaque_note}" if judge_note else opaque_note

    # Flags cap the judge component deterministically: a verdict that admits a
    # shortcoming cannot keep a "good"-band number (score-reasoning mismatch).
    if judge_component is not None:
        for flag in flags:
            cap = _JUDGE_FLAG_CAPS.get(flag)
            if cap is not None:
                judge_component = min(judge_component, cap)

    # Deterministic input-contract check: a silent rewrite of a carried-through
    # parameter (file_type, lang, format, ...) is a hard fault. Detected here
    # from the input/output diff — no LLM, no threshold guessing.
    violations = contract_violations(input_text, output_text)

    components: dict[str, float | None] = {
        "schema": schema_component,
        "judge": judge_component,
        "heuristics": heuristics_component,
    }
    score, unscored_reason = composite_score(components, weights, min_weight)
    # The judged composite BEFORE any deterministic override: when an override
    # fires, this is the "claimed" number the UI shows struck-through next to
    # the effective one — a producer whose judge praised work that a
    # reproducible check refuted must be as visible as a refuted verifier.
    judged_composite = score

    if violations:
        # A confirmed violation is decisive: it dominates a fluent judge verdict
        # and forces the node below threshold so blame localises here (cut_point).
        # The violation travels as a separate deterministic evidence stream
        # (contract_violations) — it is NOT glued into the LLM judge prose.
        components["contract"] = 0.0
        capped = _CONTRACT_VIOLATION_SCORE if score is None else min(score, _CONTRACT_VIOLATION_SCORE)
        score, unscored_reason = capped, None

    # Deterministic checks (docs/deterministic-signals.md). Every check emits
    # named signals; identity is stamped by the engine. Sources:
    # - artifact integrity: the OUT-OF-BAND artifact_meta attribute — never the
    #   payload text, which document content can forge;
    # - registered rules (check_rules table): required sections, sum invariants,
    #   tool arg schemas — filtered to this run's agent/graph_type;
    # - built-ins: unit/temporal consistency, language vs the lang/locale
    #   contract param, security scans, tool-call behavioral patterns.
    rules = [
        r
        for r in (check_rules or [])
        if r.agent_name in (None, run.agent_name)
        and r.graph_type in (None, graph_type)
    ]
    # Required sections are DOCUMENT-level requirements. An UNSCOPED rule
    # (agent_name None) applies only to the deliverable producer — a planning
    # node's correct output is an outline, and judging a plan for not containing
    # the budget table makes planners systematically come out as origins
    # (role-blind scoring). A rule explicitly scoped to an agent still applies
    # to that agent's own output wherever it sits.
    section_rules = [
        r.spec
        for r in rules
        if r.kind == "required_section"
        and (r.agent_name is not None or is_deliverable_producer)
    ]
    sum_rules = [r.spec for r in rules if r.kind == "sum_invariant"]
    tool_schemas = [r.spec for r in rules if r.kind == "tool_schema"]
    # Expected language: the carried contract param (what the INPUT asked for,
    # falling back to the node's own declared value).
    lang_keys = frozenset({"lang", "language", "locale"})
    in_params = _collect_contract_params(_try_parse_json(input_text), lang_keys)
    out_params = _collect_contract_params(_try_parse_json(output_text), lang_keys)
    expected_lang = next(
        (str(v) for v in list(in_params.values()) + list(out_params.values()) if v),
        None,
    )
    tool_calls = parse_tool_calls(run.tool_calls)

    # CONTENT checks target the WORK a node produces. A verifier's output is
    # meta-commentary about someone else's work (an English QA report about a
    # Czech deliverable, a verdict without the required sections) — running
    # content checks on it manufactures false positives, the same reason fact
    # propagation skips verifier commentary. Integrity/security/behavioral
    # checks still apply to every node.
    _name = (run.agent_name or "").lower()
    is_verifier_node = any(h in _name for h in _VERIFIER_HINTS)
    content_signals: list[dict] = []
    if not is_verifier_node:
        content_signals = (
            required_section_signals(
                output_text, section_rules, subject="this node's own output"
            )
            + sum_invariant_signals(output_text, sum_rules)
            + unit_inconsistency_signals(input_text, output_text)
            + temporal_invariant_signals(output_text, run_started_at=run.started_at)
            + language_mismatch_signals(expected_lang, output_text)
        )

    deterministic_signals = (
        artifact_integrity_signals(artifact_meta, min_bytes=min_artifact_bytes)
        + content_signals
        + sensitive_data_signals(output_text)
        + injection_signature_signals(output_text)
        + loop_fingerprint_signals(tool_calls)
        + retry_storm_signals(tool_calls)
        + duplicate_side_effect_signals(tool_calls)
        + tool_args_signals(run.tool_calls, tool_calls, tool_schemas)
    )

    # GENERALIZED deterministic-fail override: ANY fail-severity signal is a
    # hard, reproducible fact about this node's output — it caps the composite
    # below the blame threshold no matter how fluent the judge verdict was
    # (deterministic beats judge). Warn-severity signals are evidence only:
    # they ride along without touching the number.
    fail_names = list(
        dict.fromkeys(
            s["name"] for s in deterministic_signals if s["severity"] == "fail"
        )
    )
    warn_names = list(
        dict.fromkeys(
            s["name"] for s in deterministic_signals if s["severity"] == "warn"
        )
    )
    for name in fail_names + warn_names:
        if name not in flags:
            flags.append(name)
    if fail_names:
        for name in fail_names:
            components[name] = 0.0
        capped = (
            _DETERMINISTIC_FAIL_SCORE
            if score is None
            else min(score, _DETERMINISTIC_FAIL_SCORE)
        )
        score, unscored_reason = capped, None

    # Record the refuted "claimed" number whenever ANY override (contract or
    # generalized deterministic fail) lowered the judged composite — the engine
    # turns it into a score_override entry so the UI renders claimed→effective
    # for producers exactly like it does for refuted verifiers.
    if (
        judged_composite is not None
        and score is not None
        and score < judged_composite
    ):
        components["pre_override_composite"] = judged_composite

    return NodeScore(
        run_id=run_id,
        score=score,
        components=components,
        input_flawed=input_flawed,
        unscored_reason=unscored_reason,
        judge_note=judge_note,
        flags=tuple(flags),
        contract_violations=tuple(violations),
        deterministic_signals=tuple(deterministic_signals),
    )
