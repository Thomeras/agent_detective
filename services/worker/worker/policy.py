"""Shadow policy gates (roadmap 2.2) + the worker's own judge-prompt fingerprint.

``evaluate_policies`` is a PURE evaluator of the predicate DSL v1 — no IO, no
side effects, so it is unit-testable in isolation and callable from any tier.
Everything it produces is a *recorded* decision: Agent Detective analyzes after
the fact, so a firing rule means "this graph WOULD have been blocked/warned",
never "was blocked". The decision names (``would_block`` / ``would_warn``) and
every rendered detail keep that honesty; enforcement does not exist here.

Predicate DSL v1 (JSONB dict, every key optional; a rule fires when ANY of its
present conditions matches):

- ``flags_any``:        [tier1 flag names]          — any present in the verdict flags
- ``signals_any``:      [deterministic signal names] — any fired on the graph
- ``report_types_any``: [blame report types]         — the effective report type is one of them
- ``cost_over``:        <usd float>                  — graph cost exceeds the value
- ``score_below``:      <float>                      — ANY node quality_score is below the value

Malformed predicate material is skipped silently (a broken rule must never take
the analysis down): a predicate that is not a dict skips the whole rule; an
individual condition with the wrong type (e.g. ``cost_over: "cheap"``) skips
just that condition while the rule's well-formed conditions still evaluate.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from .types import PolicyDecision, PolicyRule

# Control-signal stream (roadmap 2.3): the breaker publishes RECORDED state
# transitions here. Consuming it is opt-in on the integration side — nothing
# is enforced unless the agent polls this state and decides to honor it.
STREAM_CONTROL_SIGNALS = "ad.control.signals"

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=1)
def judge_prompts_fingerprint() -> str:
    """12 hex chars of sha256 over the filename-sorted concatenation of all
    ``worker/prompts/*.md`` bytes.

    This fingerprints the WORKER'S OWN judge prompts (terminal judge, node
    judge, verifier judge, claims) so calibration can slice tier1 verdicts and
    blame reports by judge-prompt version — distinct from the agent-side
    ``prompt_hash`` (B1), which identifies the *observed agent's* prompts.
    Known limitation, stated plainly: the judge MODEL is not part of this hash
    and is not recorded anywhere else either.

    Cached for the process lifetime: prompts are packaged files, not runtime
    state.
    """
    digest = hashlib.sha256()
    for path in sorted(_PROMPTS_DIR.glob("*.md"), key=lambda p: p.name):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def evaluate_policies(
    rules: list[PolicyRule],
    *,
    flags: list[str],
    signal_names: list[str],
    report_type: str | None,
    graph_cost: float | None,
    min_node_score: float | None,
) -> list[PolicyDecision]:
    """Evaluate the predicate DSL v1 over one graph's observed facts.

    Returns one ``PolicyDecision`` per firing rule (input order preserved).
    ``detail`` states WHICH condition(s) fired with the observed value, so the
    record is auditable without re-running the evaluation. Pure function:
    persisting the decisions (and saying "would have", not "did") is the
    caller's job.
    """
    decisions: list[PolicyDecision] = []
    flag_set = set(flags)
    signal_set = set(signal_names)

    for rule in rules:
        if not rule.enabled:
            continue  # defense in depth; the repo read already filters these
        predicate = rule.predicate
        if not isinstance(predicate, dict):
            continue  # malformed predicate: skip the rule silently

        fired: list[str] = []

        flags_any = predicate.get("flags_any")
        if _is_str_list(flags_any):
            hits = [f for f in flags_any if f in flag_set]
            if hits:
                fired.append(f"flags_any: {', '.join(hits)} present in tier1 flags")

        signals_any = predicate.get("signals_any")
        if _is_str_list(signals_any):
            hits = [s for s in signals_any if s in signal_set]
            if hits:
                fired.append(
                    f"signals_any: deterministic signal(s) {', '.join(hits)} fired"
                )

        report_types_any = predicate.get("report_types_any")
        if _is_str_list(report_types_any) and report_type in report_types_any:
            fired.append(f"report_types_any: report type is '{report_type}'")

        cost_over = predicate.get("cost_over")
        if _is_number(cost_over) and graph_cost is not None and graph_cost > cost_over:
            fired.append(f"cost_over: {graph_cost:g} > {cost_over:g}")

        score_below = predicate.get("score_below")
        if (
            _is_number(score_below)
            and min_node_score is not None
            and min_node_score < score_below
        ):
            fired.append(
                f"score_below: min node quality_score {min_node_score:g} < {score_below:g}"
            )

        if fired:
            decisions.append(
                PolicyDecision(
                    rule_name=rule.name,
                    decision="would_block" if rule.action == "block" else "would_warn",
                    detail="; ".join(fired),
                )
            )
    return decisions
