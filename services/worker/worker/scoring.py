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
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from blame_engine import NodeScore, is_planner, is_verifier

from .behavioral import (
    duplicate_side_effect_signals,
    empty_output_signals,
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
from .judge_client import JudgeClient, judge_json_with_retries
from .signals import artifact_integrity_signals
from .types import (
    FLAG_UNINSPECTED_MEDIA,
    AgentStat,
    CheckRule,
    OutputContract,
    RunRecord,
)
from .narrative import render_opaque_artifact_note, render_uninspected_media_caveat

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
def node_role(agent_name: str | None, *, is_deliverable_producer: bool = False) -> str:
    """Deterministic role classification, stated to the judge as ground truth.

    VERIFIER is tested FIRST. When a name carries both hints the verifier word is
    the job and the planner word only says what is being checked — a
    ``quality_controller`` verifies, a ``plan_reviewer`` reviews. Judging either
    by the planner rubric ("its correct output is a plan") is the role-blind
    category error the rubric split exists to prevent.
    """
    if is_verifier(agent_name):
        return "VERIFIER — its correct output is a verdict on another node's work"
    if is_planner(agent_name):
        return (
            "PLANNER — its correct output is a plan/outline/routing decision; "
            "the deliverable's content is produced by later nodes"
        )
    if is_deliverable_producer:
        return (
            "DELIVERABLE PRODUCER — its output IS the artifact shipped to the "
            "user; deliverable-level requirements apply in full"
        )
    # "judge it against what its input asked THIS step to produce" USED to stand
    # here, and it was the bug: in a pipeline the input is mostly the previous
    # step's OUTPUT, not a request. The judge duly read the handoff as a spec and
    # penalised every node for not repeating its predecessor — `enrich` lost
    # points for "does not include the requested financial data" (its input
    # carried collect's financials) while its own empty result went unnoticed.
    return (
        "INTERMEDIATE PRODUCER — it adds ITS OWN contribution to a chain and "
        "hands the result on; whatever reached it from the previous step is "
        "material to work with, NOT a checklist its output must repeat"
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
# NOTE: the old localisation sentinels (_CONTRACT_VIOLATION_SCORE = 0.15,
# _DETERMINISTIC_FAIL_SCORE = 0.10) are GONE. Flooring the judged score to a
# constant below threshold multiplexed quality with localisation; deterministic
# faults now travel as their own evidence stream (contract_violations /
# deterministic_signals) and the blame engine localises on them independently.

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
#
# The extension list is SPLIT because one flat list could not tell two opposite
# situations apart and silently resolved both the harsh way. A markdown dossier
# — whose entire text WAS in the payload — illustrated itself with
# `![North wall](photos/8c5300c9.jpg)`; the .jpg matched, the whole deliverable
# was declared unverifiable and a perfectly good terminal verdict was thrown
# away. Twice, on production runs.
#
# DOCUMENT/CONTAINER formats hold text or data the payload can only paraphrase.
# A reference to one is opaque unconditionally: prose in the payload is exactly
# what a summary OF that document looks like, so prose can never be evidence
# that the document itself is present, and is not allowed to act as such.
_DOC_ARTIFACT_RE = re.compile(
    r"[\w./\\:-]+\.(?:docx|doc|pdf|pptx|ppt|xlsx|xls|zip|tar|gz|bin)\b",
    re.IGNORECASE,
)
# MEDIA formats hold no text at all, so requiring an `artifact_text` block for
# them is a requirement no honest exporter can ever meet — under the old rule an
# illustrated document was permanently unverifiable. A media reference means one
# of two things and only the surrounding payload can say which: the deliverable
# IS the picture ("here is the logo: logo.png" — still nothing to grade), or the
# deliverable is a text document that illustrates itself (checkable on its text,
# pictures excepted).
_MEDIA_ARTIFACT_RE = re.compile(
    r"[\w./\\:-]+\.(?:png|jpe?g|gif|webp|svg)\b",
    re.IGNORECASE,
)
# STRUCTURAL markers for embedded artifact content: the block form the
# instrumentation appends ("[artifact_text <path>]:") or an actual JSON field
# named artifact_text. A BARE substring is not accepted — prose that merely
# mentions the word 'artifact_text' (an agent quoting the convention, a QA note
# "artifact_text will be attached") must not self-exempt the payload from
# opacity detection.
_ARTIFACT_TEXT_MARKERS = ("[artifact_text ", '"artifact_text"')

# THE DISCRIMINATOR between "a document that illustrates itself" and "a report
# about a picture" is whether the payload CONTAINS the image at a position in
# its own body (an embed) or merely NAMES it (a path in a sentence).
#
# A word-count bar alone was tried and is not sufficient: it asks "is there a
# lot of prose here", and a verbose announcement has plenty. An 87-prose-word
# hand-off — "I have finished the logo. It uses a deep indigo ... The file is
# saved to assets/logo.png." — cleared a 60-word bar and was graded 0.95 on
# prose whose every sentence is a claim ABOUT a file nobody opened. That is the
# document-axis sin ("prose is exactly what a summary looks like") relocated to
# images, and it is the false-CERTAINTY direction, so the bar cannot stand on
# its own. An embed is a structural fact about the payload, not a size proxy:
# writing `![North wall detail](photos/x.jpg)` composes a document, writing
# "the file is saved to assets/logo.png" files a report.
#
# Unrecognised embed spellings fail SAFE — an embed we cannot parse looks like a
# bare mention, so the payload is treated as opaque and the verdict is withheld
# rather than invented. The word bar is still required ON TOP of the embed (see
# the gallery case below), never instead of it.
_MARKDOWN_EMBED_RE = re.compile(r"!\[[^\]\n]*\]\(\s*<?([^)\s>]+)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']?([^\"'>\s]+)", re.IGNORECASE)
# Whole-construct forms, used to strike embeds out of the body-prose count.
_MARKDOWN_EMBED_FULL_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_HTML_IMG_FULL_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

# Words of the payload's OWN body (every artifact reference and every embed
# removed first) below which there is no document to grade — the payload is an
# announcement about a file, not the file. Measured in WORDS, not bytes, so that
# a bare gallery listing of sixty image paths (kilobytes of text, zero body)
# cannot buy its way past the bar. Calibrated on how the two cases actually
# talk: a step whose deliverable IS a picture announces it in a sentence or two,
# while a step that wrote a document ships paragraphs. This is a CALIBRATION
# JUDGEMENT, not a measurement — it was never validated against a corpus of real
# payloads. It is a second condition, never a sufficient one.
_MIN_DELIVERABLE_PROSE_WORDS = 60
_PROSE_WORD_RE = re.compile(r"[^\W\d_]{2,}")

# The three states a payload's artifact evidence can be in. Two states could not
# say the true thing about a multimodal deliverable: a dossier whose value is
# partly in photographs is neither "verified" nor "unverifiable", it is verified
# on its text with the image content never inspected. PARTIAL is that verdict,
# and it carries the limit with it so a pass can never be read as a full one.
ARTIFACT_VISIBLE = "visible"
ARTIFACT_PARTIAL = "partial"
ARTIFACT_OPAQUE = "opaque"


@dataclass(frozen=True)
class ArtifactVisibility:
    """How much of the work a payload claims is actually IN the payload.

    ``state`` is one of ``visible`` / ``partial`` / ``opaque``.
    ``opaque_refs`` are the references that leave nothing to grade (set on
    ``opaque`` only); ``uninspected_refs`` are media inside a payload we CAN
    read but whose content nobody saw (set on ``partial`` only).
    """

    state: str
    opaque_refs: tuple[str, ...] = ()
    uninspected_refs: tuple[str, ...] = ()


def _refs(pattern: re.Pattern[str], text: str) -> list[str]:
    """Distinct matches in first-seen order."""
    return list(dict.fromkeys(m.group(0) for m in pattern.finditer(text)))


def _embedded_media_refs(text: str) -> list[str]:
    """Media the payload EMBEDS rather than merely names.

    Markdown `![alt](path)` and HTML `<img src=…>`. A path that appears only
    inside running prose is NOT an embed: the payload is pointing somewhere
    else, and pointing is exactly the situation the opacity rule withholds a
    verdict for.
    """
    found: list[str] = []
    for pattern in (_MARKDOWN_EMBED_RE, _HTML_IMG_RE):
        for match in pattern.finditer(text):
            src = match.group(1)
            if _MEDIA_ARTIFACT_RE.fullmatch(src):
                found.append(src)
    return list(dict.fromkeys(found))


def _body_prose_word_count(text: str) -> int:
    """Words of the payload's OWN body: artifact references struck out, and
    every image embed struck out WHOLE — alt text included.

    Alt text is a caption of a picture nobody opened, i.e. a claim ABOUT the
    artifact, so it can no more prove a readable deliverable is present than
    prose about a .docx can prove the .docx is present. Counting it let a real
    12-image markdown gallery — 72 words, every one of them a caption, zero
    body — present itself as a gradeable document and be scored on its captions.
    """
    remainder = _HTML_IMG_FULL_RE.sub(" ", _MARKDOWN_EMBED_FULL_RE.sub(" ", text))
    remainder = _MEDIA_ARTIFACT_RE.sub(" ", _DOC_ARTIFACT_RE.sub(" ", remainder))
    return len(_PROSE_WORD_RE.findall(remainder))


def classify_artifact_visibility(output_text: str | None) -> ArtifactVisibility:
    """Decide how much of the claimed work this payload actually shows.

    ``opaque`` — the payload is a POINTER: a document/container reference (always,
    prose can never stand in for one), or media that is only named. Nothing the
    judge reads is the work, so no verdict may be issued on it.

    ``partial`` — the payload's own body IS a readable deliverable and it embeds
    images inside itself. The text can be graded; the pictures were never
    opened, and ``uninspected_refs`` records exactly which, so the grade is
    reported as what it is rather than as full verification.

    ``visible`` — no artifact reference at all, or the instrumentation embedded
    the artifact's extracted text (``artifact_text``) and there are no images
    beside it.
    """
    if not output_text:
        return ArtifactVisibility(ARTIFACT_VISIBLE)
    if any(m in output_text for m in _ARTIFACT_TEXT_MARKERS):
        # The instrumentation embedded the artifact's extracted TEXT, so nothing
        # is opaque. Images alongside it are still images: extraction produces no
        # pixels, so a payload that ships a document's text and points at its
        # figures is verified on the text and silent about the figures — the
        # same partial verdict, reached by a different route.
        media_refs = _refs(_MEDIA_ARTIFACT_RE, output_text)
        if media_refs:
            return ArtifactVisibility(ARTIFACT_PARTIAL, uninspected_refs=tuple(media_refs))
        return ArtifactVisibility(ARTIFACT_VISIBLE)
    doc_refs = _refs(_DOC_ARTIFACT_RE, output_text)
    if doc_refs:
        return ArtifactVisibility(ARTIFACT_OPAQUE, opaque_refs=tuple(doc_refs))
    media_refs = _refs(_MEDIA_ARTIFACT_RE, output_text)
    if not media_refs:
        return ArtifactVisibility(ARTIFACT_VISIBLE)
    # BOTH conditions, deliberately: the embed says the images live inside this
    # payload, the body count says the payload has something of its own besides
    # them. Either alone is defeated by a real artifact class — a bare mention
    # in a long report, and a caption-only photo gallery, respectively.
    if (
        _embedded_media_refs(output_text)
        and _body_prose_word_count(output_text) >= _MIN_DELIVERABLE_PROSE_WORDS
    ):
        return ArtifactVisibility(ARTIFACT_PARTIAL, uninspected_refs=tuple(media_refs))
    return ArtifactVisibility(ARTIFACT_OPAQUE, opaque_refs=tuple(media_refs))


def opaque_artifact_refs(output_text: str | None) -> list[str]:
    """File-artifact references that leave the payload UNGRADEABLE.

    Thin view over :func:`classify_artifact_visibility` (kept because the CLI
    doctor imports it): non-empty exactly when the payload is ``opaque``.
    """
    return list(classify_artifact_visibility(output_text).opaque_refs)


def uninspected_media_refs(output_text: str | None) -> list[str]:
    """Media inside a gradeable payload that nobody opened.

    Non-empty exactly when the payload is ``partial``. Empty when it is opaque:
    there the whole verdict is withheld and a caveat would be noise. These
    images do not block grading, but they are not free either — whatever they
    show was never seen, so callers record them and the pass reads "verified on
    the text" instead of the stronger claim "verified".
    """
    return list(classify_artifact_visibility(output_text).uninspected_refs)


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


def _collect_contract_params(
    obj: object, keys: frozenset[str]
) -> dict[str, list[object]]:
    """Recursively collect every DISTINCT scalar value seen for each contract key.

    Distinctness is by :func:`_norm` (the same tolerant comparison the violation
    check uses) while the first spelling of each value is what is kept, so the
    reported triple quotes the payload rather than a casefolded rewrite.

    All values, not the first one, because a FAN-IN node's input carries one
    value per incoming branch. Collapsing those to whichever the walk happened to
    reach first invented a contract the node was never given: with branches
    ``lang=cs`` and ``lang=en`` merging into a ``lang=cs`` output, "the first
    scalar" is a coin flip that either hides a rewrite or manufactures one, and a
    manufactured one makes the joiner look like the origin of a contract breach.
    :func:`_unambiguous_contract_params` is where that is resolved — into
    "unknown", never into a pick.
    """
    found: dict[str, list[object]] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in keys and not isinstance(v, (dict, list)):
                    seen = found.setdefault(k.lower(), [])
                    if all(_norm(v) != _norm(existing) for existing in seen):
                        seen.append(v)
                walk(v)
        elif isinstance(node, list):
            for el in node:
                walk(el)

    walk(obj)
    return found


def _unambiguous_contract_params(
    collected: dict[str, list[object]]
) -> dict[str, object]:
    """Keep only the keys that carried exactly ONE distinct value.

    A key that arrived with two conflicting values does not tell us what the
    contract WAS, so nothing can be said about whether the node honoured it.
    Dropping it leaves the fact unknown; keeping an arbitrary one of the two
    would turn "we cannot tell" into a named violation with a named culprit —
    the one failure mode this system exists to refuse.
    """
    return {k: v[0] for k, v in collected.items() if len(v) == 1}


def _declared_contract_params(declared: str | None) -> dict[str, object]:
    """Tolerant parse of the ``agent_detective.contract_params`` attribute.

    A JSON object of scalar values ({"file_type": "pdf"}); anything else —
    malformed JSON, non-object, nested/None values — is ignored, because a
    broken declaration must neither invent nor suppress violations.
    """
    parsed = _try_parse_json(declared)
    if not isinstance(parsed, dict):
        return {}
    return {
        k.lower(): v
        for k, v in parsed.items()
        if isinstance(k, str) and k.strip() and v is not None and not isinstance(v, (dict, list))
    }


def contract_violations(
    input_text: str | None,
    output_text: str | None,
    keys: frozenset[str] = _CONTRACT_KEYS,
    declared: str | None = None,
) -> list[tuple[str, object, object]]:
    """Contract parameters present in BOTH input and output whose value changed.

    Returns ``(key, input_value, output_value)`` triples. Empty when input or
    output is not JSON, when every shared contract parameter was preserved, or
    when a parameter arrived (or left) with conflicting values — a fan-in node
    merging ``lang=cs`` and ``lang=en`` branches has no single contract to have
    violated, and guessing one names a culprit on no evidence.

    ``declared`` is the run's out-of-band ``agent_detective.contract_params``
    attribute: the convention lane for pipelines whose input payloads are
    prose/code and give the JSON walk nothing to parse (the measured foreign
    wild-trace gap). Declared params are the input side of the contract for
    their keys — set by instrumentation, so they override the forgeable
    payload parse — and they widen the OUTPUT-side key search to themselves,
    so a declared key outside the built-in list is still checked. The output
    value must still be observable in the output payload; without it there is
    no diff and no violation.
    """
    declared_params = _declared_contract_params(declared)
    parsed_in = _try_parse_json(input_text)
    parsed_out = _try_parse_json(output_text)
    in_params: dict[str, object] = {}
    if isinstance(parsed_in, (dict, list)):
        in_params = _unambiguous_contract_params(_collect_contract_params(parsed_in, keys))
    in_params.update(declared_params)
    if not in_params or not isinstance(parsed_out, (dict, list)):
        return []
    out_keys = keys | frozenset(declared_params)
    out_params = _unambiguous_contract_params(_collect_contract_params(parsed_out, out_keys))
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

    # An absent payload and an EMPTY one carry the same information: there is
    # nothing to measure. Scoring "" produced a hard 0.0 (heuristics floors
    # empty output, and the judge rates emptiness as worthless), i.e. the
    # strongest possible claim — "demonstrably bad" — from the weakest possible
    # evidence. That is the same error the engine refuses everywhere else
    # (absent -> unknown, never assumed healthy *or* broken); it also fabricated
    # a culprit out of orchestrator wrapper spans, which legitimately carry no
    # output of their own and are the single most common shape in real
    # framework traces. If an empty output IS the defect, that belongs to the
    # deterministic channel (a signal, a failed status, a terminal verdict) —
    # not to a quality scalar inferred from nothing.
    #
    # The observation is NOT dropped, it is re-routed: a non-root node landing
    # here raises the `instrumentation_warning` note ("these nodes have no
    # output payload — fix the exporter"), which says what is actually true
    # ("we were blinded here") instead of accusing the agent.
    #
    # Unless the run itself says otherwise. A payload that was RECORDED empty
    # while usage reports emitted tokens is not a blind spot — the exporter
    # worked and what it captured is an agent that spent its budget and
    # returned nothing. That goes out on the deterministic channel (below), the
    # one this comment has always pointed at, so it lands as a defect at the
    # node instead of as advice to fix a healthy exporter. The score stays
    # UNKNOWN either way; a signal is not a quality scalar.
    if output_text is None or not output_text.strip():
        empty_signals = empty_output_signals(output_text, run.tokens_out)
        return NodeScore(
            run_id=run_id,
            score=None,
            components={"schema": None, "judge": None, "heuristics": None},
            input_flawed=None,
            unscored_reason="empty_output" if empty_signals else "payload_missing",
            judge_note=None,
            flags=tuple(s["name"] for s in empty_signals),
            deterministic_signals=tuple(empty_signals),
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

    # Deterministic artifact-visibility check: what fraction of the work this
    # payload claims is actually IN the payload. One classification, three
    # outcomes — a judge verdict on a pointer is an assertion about an unopened
    # file, and a judge verdict on an illustrated document is true of its text
    # and silent about its pictures.
    visibility = classify_artifact_visibility(output_text)
    if visibility.state == ARTIFACT_OPAQUE and "unverifiable_artifact" not in flags:
        flags.append("unverifiable_artifact")
        opaque_note = render_opaque_artifact_note(list(visibility.opaque_refs))
        judge_note = f"{judge_note} | {opaque_note}" if judge_note else opaque_note
    elif visibility.state == ARTIFACT_PARTIAL:
        # PARTIAL: the node's own text was graded, so no cap — capping every
        # illustrated document at 0.60 would penalise the format, and inventing
        # a penalty out of "we did not look" is the mirror of inventing a pass.
        # The limit is instead RECORDED, twice: in the note (for humans) and as
        # a flag (for anything that reads the score downstream), so a good
        # number can never be read as "the photographs checked out too".
        media_note = render_uninspected_media_caveat(list(visibility.uninspected_refs))
        judge_note = f"{judge_note} | {media_note}" if judge_note else media_note
        if FLAG_UNINSPECTED_MEDIA not in flags:
            flags.append(FLAG_UNINSPECTED_MEDIA)

    # Flags cap the judge component deterministically: a verdict that admits a
    # shortcoming cannot keep a "good"-band number (score-reasoning mismatch).
    if judge_component is not None:
        for flag in flags:
            cap = _JUDGE_FLAG_CAPS.get(flag)
            if cap is not None:
                judge_component = min(judge_component, cap)

    # Deterministic input-contract check: a silent rewrite of a carried-through
    # parameter (file_type, lang, format, ...) is a hard fault. Detected here
    # from the input/output diff — no LLM, no threshold guessing. Out-of-band
    # declared params (migration 0011) stand in for the input side when the
    # payload is prose/code.
    violations = contract_violations(input_text, output_text, declared=run.contract_params)

    components: dict[str, float | None] = {
        "schema": schema_component,
        "judge": judge_component,
        "heuristics": heuristics_component,
    }
    score, unscored_reason = composite_score(components, weights, min_weight)
    # CHANNEL DECOUPLING: the judged score is NEVER floored to a localisation
    # sentinel. A deterministic fault (contract violation, fail-severity signal)
    # is a SEPARATE evidence stream (contract_violations / deterministic_signals)
    # that the blame engine reads as an independent candidacy channel — it must
    # not be smuggled into the quality scalar. Flooring the score to 0.15/0.10
    # multiplexed "how good is the output" with "a check failed here": it buried
    # the judged number (a fluent verdict on genuinely broken work is the product,
    # not noise) and made "score < threshold" a tautology the UI then re-sold as a
    # measurement. The violation still travels below via `violations`; it just no
    # longer overwrites the number.

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
    # falling back to the node's own declared value). Ambiguous keys are dropped
    # first — a fan-in whose branches carried different languages has no single
    # expected language, and picking one would report every node downstream of
    # the merge as writing in the wrong one.
    lang_keys = frozenset({"lang", "language", "locale"})
    in_params = _unambiguous_contract_params(
        _collect_contract_params(_try_parse_json(input_text), lang_keys)
    )
    out_params = _unambiguous_contract_params(
        _collect_contract_params(_try_parse_json(output_text), lang_keys)
    )
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
    is_verifier_node = is_verifier(run.agent_name)
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
    # No score flooring here either (see CHANNEL DECOUPLING above): fail-severity
    # signals ride out as flags + deterministic_signals and localise blame through
    # the engine's deterministic channel, leaving the judged score untouched.

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
