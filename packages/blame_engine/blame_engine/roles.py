"""Node role classification — ONE home for the agent-name heuristics.

Until now the verifier hints lived as three independent literal tuples
(``blame.py``, worker ``tier2.py``, worker ``graph_ops.py``) and the planner
hints as a fourth in worker ``scoring.py``. They agreed by convention only,
while four separate decisions leaned on them: which node is a verifier (blame),
which run is the deliverable (tier1/tier2), which rubric the judge gets
(scoring), and how the UI groups the score map. A silent divergence between two
of those copies is a wrong verdict, not a cosmetic bug — so the hints live here
and every caller imports them.

Matching is TOKEN-based, not substring-based. The old ``any(h in name)`` test
made ``delegate``, ``investigate_agent`` and ``checkout_agent`` verifiers
(``gate`` / ``check`` appear inside ordinary words) while missing ``judge``,
``tester``, ``inspector``, ``grader``, ``linter``, ``moderator`` and
``approver`` entirely. Verifier hints are short and ambiguous (``qa``,
``gate``), so they match whole tokens; planner hints are long and distinctive,
so they match token PREFIXES (``orchestrat`` → ``orchestrator``) while still
refusing ``floorplan_renderer`` as a planner.

The vocabulary is ENGLISH, and deliberately so: it covers the role names the
agent frameworks actually emit (CrewAI role strings like ``Software Quality
Control Engineer``, LangGraph/AutoGen node names like ``supervisor``,
``critic``, ``planner``). It is not a language-detection layer. The sets are
module constants precisely so a deployment with domain-specific or non-English
names extends them — ``roles.VERIFIER_TOKENS |= {"kontrolor", "recenzent"}`` —
instead of forking the logic.

Additions are weighed against FALSE positives, because a producer mistaken for a
verifier is skipped by content checks, walked past when picking the deliverable,
and judged by the wrong rubric. Ambiguous ordinary English words stay OUT
(``lead`` would swallow a lead-generation agent, ``monitor`` an agent that only
reports data, ``compliance`` a pipeline stage that writes the compliance
section).
"""

from __future__ import annotations

import re

__all__ = [
    "VERIFIER_TOKENS",
    "VERIFIER_SUBSTRINGS",
    "VERIFIER_PHRASES",
    "PLANNER_PREFIXES",
    "is_verifier",
    "is_planner",
    "tokens",
]

# Whole tokens that name a verifier/gate node — one whose OUTPUT is a verdict on
# another node's work, not a deliverable.
VERIFIER_TOKENS: set[str] = {
    "qa", "qc",
    "eval", "evals", "evaluate", "evaluator", "evaluation",
    "review", "reviews", "reviewer", "critique", "critic", "criticism",
    "verify", "verifier", "verification", "verified",
    "validate", "validator", "validation",
    "check", "checks", "checker", "checking",
    "audit", "audits", "auditor", "auditing",
    "gate", "gates", "gatekeeper", "gating",
    "judge", "judgement", "judgment", "adjudicator",
    "test", "tests", "tester", "testing",
    "inspect", "inspector", "inspection",
    "grade", "grader", "grading",
    "lint", "linter", "linting",
    "guard", "guardrail", "guardrails",
    "moderate", "moderator", "moderation",
    "approve", "approver", "approval",
    "assess", "assessor", "assessment",
    "referee", "arbiter", "arbitrator", "adjudicate",
    # Scoring/ranking lanes: an LLM-as-judge node is as often called a scorer or
    # a rater as it is called a judge.
    "scorer", "scoring", "rater", "rating", "ranker", "ranking",
    # Screening/watch lanes.
    "screener", "screening", "examiner", "examination", "watchdog", "sentinel",
    "jury", "qc",
}

# Long, unambiguous stems matched against the WHOLE name as a fallback, so a
# name with no token separators at all (``qaGATE`` normalizes fine, ``verifystep``
# does not) is still recognised. Deliberately excludes the short/ambiguous stems
# (``qa``, ``gate``, ``check``, ``test``, ``eval``) — those are exactly the ones
# that produced the ``delegate`` / ``checkout`` false positives.
VERIFIER_SUBSTRINGS: tuple[str, ...] = (
    "verif", "validat", "review", "audit", "critic", "evaluat", "inspect",
)

# Multi-word role titles (CrewAI-style ``Quality Assurance Engineer``), matched
# against the normalized, space-joined token stream.
VERIFIER_PHRASES: tuple[str, ...] = (
    "quality assurance",
    "quality control",
    "fact check",
    "red team",
    "peer review",
    "code review",
    "acceptance test",
)

# Planner/orchestration stems: the node's correct output is a plan or a routing
# decision, never the deliverable's content. Matched as token PREFIXES.
#
# ``manag``/``delegat``/``controll`` are the framework words for the node that
# hands work out rather than doing it. ``delegate`` used to be read as a VERIFIER
# (the substring ``gate``) — the opposite role. Left out on purpose: ``lead``
# (lead-generation agents), ``architect`` and ``designer`` (their output IS the
# deliverable), ``head``/``chief`` (seniority, not role).
PLANNER_PREFIXES: tuple[str, ...] = (
    "think", "plan", "orchestrat", "rout", "coordinat", "supervis", "dispatch",
    "manag", "delegat", "schedul", "triage", "director", "controll",
)

_SPLIT_RE = re.compile(r"[^0-9a-zA-Z]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokens(name: str | None) -> list[str]:
    """Lowercased word tokens of an agent name, splitting on separators AND
    camelCase humps (``qaGate`` -> ``['qa', 'gate']``)."""
    if not name:
        return []
    out: list[str] = []
    for part in _SPLIT_RE.split(name):
        if not part:
            continue
        out.extend(t.lower() for t in _CAMEL_RE.split(part) if t)
    return out


def is_verifier(name: str | None) -> bool:
    """True when the agent name identifies a verifier/gate node."""
    toks = tokens(name)
    if any(t in VERIFIER_TOKENS for t in toks):
        return True
    if any(p in " ".join(toks) for p in VERIFIER_PHRASES):
        return True
    lowered = (name or "").lower()
    return any(s in lowered for s in VERIFIER_SUBSTRINGS)


def is_planner(name: str | None) -> bool:
    """True when the agent name identifies a planner/orchestrator node."""
    return any(t.startswith(p) for t in tokens(name) for p in PLANNER_PREFIXES)
