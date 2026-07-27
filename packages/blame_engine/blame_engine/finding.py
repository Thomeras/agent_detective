"""Finding — a typed fact with provenance (verdict refactor §2.1).

A Finding is what a detector EMITS: a measured fact, its evidential channel, the
subject it is about, a kind-specific payload, WHERE the reference/basis came from
(provenance sum type) and how certain the measurement is. Detectors only emit;
adding a detector is one new emitter with zero downstream changes.

Findings are pure data. They serialize to plain JSON-friendly dicts via
``serialize_finding`` (the Evidence schema-2 payload stores those dicts, so the
worker's ``asdict(evidence)`` passes them through unchanged).

The ``fact_key`` is the identity used by the reconcile pass (§2.4): two findings
that measure the SAME fact carry the same key, so a divergence between them can
never be printed as two unreconciled values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

# --- Provenance sum type -------------------------------------------------
# WHERE the reference / basis of a finding came from. The distinction is
# load-bearing: a contract reference that is harness scaffold must never be read
# as "the user asked for this" (the report-#1 requirement_provenance bug).


@dataclass(frozen=True)
class UserRequest:
    """The basis is a verbatim quote of the user's initial request."""

    quote: str
    source: str = "initial_input"


@dataclass(frozen=True)
class HarnessState:
    """The basis is harness/orchestrator scaffold, not the user's ask."""

    detail: str = ""


@dataclass(frozen=True)
class Upstream:
    """The basis is a value produced by an upstream run."""

    run_id: str
    detail: str = ""


@dataclass(frozen=True)
class RuleFingerprint:
    """The basis is a deterministic rule (contract/check) fingerprint."""

    rule: str
    detail: str = ""


@dataclass(frozen=True)
class JudgePrompt:
    """The basis is an LLM judge prompt (identified by a stable hash)."""

    prompt: str = ""
    detail: str = ""


Provenance = UserRequest | HarnessState | Upstream | RuleFingerprint | JudgePrompt

# ``detail`` on a provenance is an identifier for WHICH source produced the
# basis, never a sentence (§2.4). The label table below is the one place those
# codes become English — the same discipline the notes/candidacy use, applied to
# the layer under them. Legacy payloads hold prose here; an unknown code is
# rendered verbatim, so stored reports keep reading as they did.
PROV_PER_NODE_QUALITY_JUDGE = "per_node_quality_judge"
PROV_PER_NODE_QUALITY_DELTA = "per_node_quality_delta"
PROV_TERMINAL_JUDGE_CONTENT = "terminal_judge_content"
PROV_TERMINAL_JUDGE_FORM = "terminal_judge_form"
PROV_VERIFIER_JUDGE = "verifier_judge"
PROV_VERIFIER_CHARTER_ROSTER = "verifier_charter_roster"
PROV_RECONCILE_FACT_KEY = "reconcile_fact_key"
PROV_DELIVERABLE_PAYLOAD = "deliverable_payload"
PROV_INPUT_OUTPUT_DIFF = "input_output_diff"
PROV_REQUIRED_SECTION_CHECK = "required_section_check"
PROV_CONTRACT_PROPAGATION = "contract_propagation_check"

PROVENANCE_LABELS: dict[str, str] = {
    PROV_PER_NODE_QUALITY_JUDGE: "per-node quality judge",
    PROV_PER_NODE_QUALITY_DELTA: (
        "per-node quality judge (score delta across the node)"
    ),
    PROV_TERMINAL_JUDGE_CONTENT: "tier1 terminal judge (content rubric)",
    PROV_TERMINAL_JUDGE_FORM: "tier1 terminal judge (form rubric)",
    PROV_VERIFIER_JUDGE: "role-aware verifier judge / deduction",
    PROV_VERIFIER_CHARTER_ROSTER: "verifier charter roster",
    PROV_RECONCILE_FACT_KEY: "reconcile pass over shared fact_key",
    PROV_DELIVERABLE_PAYLOAD: "deliverable payload inspection",
    PROV_INPUT_OUTPUT_DIFF: "input/output diff on a carried parameter",
    PROV_REQUIRED_SECTION_CHECK: "required_section check on one representation",
    PROV_CONTRACT_PROPAGATION: "contract-propagation check on the deliverable",
}


def provenance_label(p: Provenance) -> str:
    """Human label for a provenance: the rule/quote it rests on plus the
    resolved ``detail`` code. The ONLY place a provenance becomes English."""
    detail = getattr(p, "detail", "") or ""
    resolved = PROVENANCE_LABELS.get(detail, detail)
    if isinstance(p, UserRequest):
        return f"user request ({p.source}): {p.quote!r}"
    if isinstance(p, RuleFingerprint):
        return f"rule {p.rule}" + (f" — {resolved}" if resolved else "")
    if isinstance(p, Upstream):
        return f"upstream run {p.run_id}" + (f" — {resolved}" if resolved else "")
    if isinstance(p, JudgePrompt):
        return resolved or (f"judge prompt {p.prompt}" if p.prompt else "judge")
    return resolved or "harness state"

Channel = Literal["deterministic", "judged"]

# Subject is a small tagged string: "run:<id>" | "terminal" | "graph".
Subject = str


@dataclass(frozen=True)
class Finding:
    kind: str                      # catalogued (see §2.2 / FINDING_KINDS)
    channel: Channel               # "deterministic" | "judged"
    subject: Subject               # run:<id> | terminal | graph
    data: Mapping[str, Any]        # kind-specific typed payload (JSON-friendly)
    provenance: Provenance         # where the reference/basis came from
    certainty: float               # 1.0 deterministic; calibrated for judged
    # Reconcile identity (§2.4). Findings measuring the same fact share a key;
    # None when the finding measures nothing another finding could contradict.
    fact_key: str | None = None


# Catalogue of finding kinds this engine emits (§2.1 mapping table). Kept as a
# frozenset so a typo in an emitter is catchable, without constraining the type.
FINDING_KINDS = frozenset(
    {
        "contract_breach",
        "artifact_integrity",
        "deterministic_signal",
        "content_score",
        "content_flag",
        "content_drop",          # measured score delta across a node (localization fact)
        "input_flawed",          # a source's own judge reported its INPUT already flawed
        "terminal_content",
        "terminal_form",
        "verifier_verdict",
        "detection_gap",         # design-level: no pipeline verifier owns this dimension
        "loop_anomaly",
        # worker-side representations (F2.2 extra_findings)
        "required_section",      # section presence per representation (producers/deliverable)
        "breach_propagated",     # contract breach VERIFIED in the shipped deliverable
        "breach_corrected",      # contract breach verified corrected downstream
        # reconcile outputs (§2.4)
        "representation_divergence",
        "requirement_provenance",
        "assessment_conflict",
    }
)

# --- file-type token normalization ---------------------------------------
# Shared by the terminal_form fact_key emission and the form-defect anchor: a
# requirement quote ("jako PDF") and a contract param ("docx") only reconcile
# if both sides reduce to the same token space. Without this the most important
# divergence in the data — requirement (pdf) vs contract scaffold (docx) — sits
# silently side by side with full provenance on both (the report-#2 chain).

_FILE_TYPE_TOKENS = frozenset(
    {"pdf", "docx", "doc", "md", "markdown", "html", "htm", "csv",
     "xlsx", "pptx", "txt", "json"}
)
_FILE_TYPE_CANON = {"markdown": "md", "doc": "docx", "htm": "html"}


def file_type_token(text: str | None) -> str | None:
    """The normalized file-type token a short phrase mentions, or None.

    'jako PDF' -> 'pdf'; 'markdown text' -> 'md'; 'a coherent report' -> None.
    First match wins — requirement phrases name one format.
    """
    if not text:
        return None
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _FILE_TYPE_TOKENS:
            return _FILE_TYPE_CANON.get(word, word)
    return None


def run_subject(run_id: str) -> str:
    return f"run:{run_id}"


def _serialize_provenance(p: Provenance) -> dict:
    """Tagged serialization so the provenance KIND survives the JSON round-trip
    (a plain asdict would lose which member of the union it was).

    ``label`` is the rendered form, carried alongside the codes so a UI can show
    it without shipping the table — the same record+rendering pair the notes
    use. It is never read back: ``deserialize_provenance`` ignores it.
    """
    tag = type(p).__name__
    return {
        "kind": tag,
        **{k: v for k, v in vars(p).items()},
        "label": provenance_label(p),
    }


def serialize_finding(f: Finding) -> dict:
    return {
        "kind": f.kind,
        "channel": f.channel,
        "subject": f.subject,
        "data": dict(f.data),
        "provenance": _serialize_provenance(f.provenance),
        "certainty": f.certainty,
        "fact_key": f.fact_key,
    }


def deserialize_provenance(p: dict) -> Provenance:
    kind = p.get("kind")
    if kind == "UserRequest":
        return UserRequest(quote=p.get("quote", ""), source=p.get("source", "initial_input"))
    if kind == "HarnessState":
        return HarnessState(detail=p.get("detail", ""))
    if kind == "Upstream":
        return Upstream(run_id=p.get("run_id", ""), detail=p.get("detail", ""))
    if kind == "RuleFingerprint":
        return RuleFingerprint(rule=p.get("rule", ""), detail=p.get("detail", ""))
    if kind == "JudgePrompt":
        return JudgePrompt(prompt=p.get("prompt", ""), detail=p.get("detail", ""))
    raise ValueError(f"unknown provenance kind: {kind!r}")


def deserialize_finding(f: dict) -> Finding:
    """Reconstruct a Finding from its serialized form (cassettes / stored
    schema-2 evidence)."""
    return Finding(
        kind=f["kind"],
        channel=f["channel"],
        subject=f["subject"],
        data=dict(f.get("data") or {}),
        provenance=deserialize_provenance(f["provenance"]),
        certainty=f["certainty"],
        fact_key=f.get("fact_key"),
    )
