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

from .judge_client import JudgeClient, judge_json_with_retries
from .types import AgentStat, OutputContract, RunRecord

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Deterministic judge truncation window (spec 4.3 step 2).
_JUDGE_HEAD_BYTES = 12 * 1024
_JUDGE_TAIL_BYTES = 4 * 1024

_WORD_RE = re.compile(r"\S+")

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
    prompt = render_prompt(
        judge_prompt_template,
        {
            "AGENT_NAME": run.agent_name or "unknown",
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

    if violations:
        # A confirmed violation is decisive: it dominates a fluent judge verdict
        # and forces the node below threshold so blame localises here (cut_point).
        components["contract"] = 0.0
        detail = "; ".join(f"{k}: {a!r}->{b!r}" for k, a, b in violations)
        contract_note = f"input contract violated (silent parameter rewrite): {detail}"
        judge_note = f"{judge_note} | {contract_note}" if judge_note else contract_note
        capped = _CONTRACT_VIOLATION_SCORE if score is None else min(score, _CONTRACT_VIOLATION_SCORE)
        score, unscored_reason = capped, None

    return NodeScore(
        run_id=run_id,
        score=score,
        components=components,
        input_flawed=input_flawed,
        unscored_reason=unscored_reason,
        judge_note=judge_note,
    )
