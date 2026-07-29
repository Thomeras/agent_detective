"""Deterministic behavioral trace signals (docs/deterministic-signals.md, A3).

Input is the parsed tool-call digest: a list of ``{name, args_sha, status}``
dicts in call order (an optional ``args`` key carries the raw arguments when
the mapper provides them — forward-compat, see ``tool_args_signals``).
``parse_tool_calls`` turns the raw attribute text into that shape, tolerant in
the style of ``worker.signals.parse_artifact_meta``: malformed input yields
``[]``, never an exception.

Signals emitted here:

- ``loop_fingerprint``      (warn) — identical call repeated consecutively;
- ``retry_storm``           (warn) — identical call repeated with errors;
- ``duplicate_side_effect`` (fail) — side-effecting call executed more than
  once with identical args and every attempt succeeded: the email really went
  out twice, which IS a production incident, hence fail;
- ``tool_args_invalid``     (fail) — raw args violate a registered tool schema;
- ``cost_anomaly`` / ``token_anomaly`` (warn) — z-score >= 3 vs the agent's
  rolling baseline;
- ``empty_output``          (fail) — the run recorded an empty output while its
  own usage says the model emitted tokens: it spent and shipped nothing.

All functions are pure and identity-free; the caller stamps run_id/agent.
"""

from __future__ import annotations

import json
from typing import Any

from .types import AgentStat
from .narrative import signal

SIGNAL_LOOP_FINGERPRINT = "loop_fingerprint"
SIGNAL_RETRY_STORM = "retry_storm"
SIGNAL_DUPLICATE_SIDE_EFFECT = "duplicate_side_effect"
SIGNAL_TOOL_ARGS_INVALID = "tool_args_invalid"
SIGNAL_COST_ANOMALY = "cost_anomaly"
SIGNAL_TOKEN_ANOMALY = "token_anomaly"
SIGNAL_EMPTY_OUTPUT = "empty_output"

DEFAULT_SIDE_EFFECT_MARKERS: tuple[str, ...] = (
    "send",
    "mail",
    "pay",
    "post",
    "write",
    "delete",
    "create",
    "submit",
)

_ZSCORE_THRESHOLD = 3.0
_MIN_BASELINE_SAMPLES = 5


def parse_tool_calls(text: str | None) -> list[dict]:
    """Parse the tool-call digest attribute value.

    Accepts a JSON array of ``{name, args_sha, status}`` dicts (the contract)
    or a single dict (wrapped for tolerance). Missing ``name`` becomes ``"?"``,
    missing ``args_sha`` becomes ``""`` — an entry should not vanish because
    the emitter forgot a field. A raw ``args`` key is passed through verbatim
    when present. Malformed JSON -> ``[]``; never raises.
    """
    if not text or not isinstance(text, str):
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    results: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        args_sha = entry.get("args_sha")
        status = entry.get("status")
        normalized: dict[str, Any] = {
            "name": name if isinstance(name, str) and name.strip() else "?",
            "args_sha": args_sha if isinstance(args_sha, str) else "",
            "status": status if isinstance(status, str) else None,
        }
        if "args" in entry:
            normalized["args"] = entry["args"]
        results.append(normalized)
    return results


def _entries(tool_calls: object) -> list[dict]:
    """Defensive view: only dict entries with a usable name/args_sha pair."""
    if not isinstance(tool_calls, list):
        return []
    out: list[dict] = []
    for entry in tool_calls:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        args_sha = entry.get("args_sha")
        if not isinstance(name, str) or not isinstance(args_sha, str):
            continue
        out.append(entry)
    return out


def _key(entry: dict) -> tuple[str, str]:
    return (entry["name"], entry["args_sha"])


def loop_fingerprint_signals(tool_calls: list[dict] | None) -> list[dict]:
    """``loop_fingerprint`` warn for every streak of >= 2 CONSECUTIVE calls
    with identical ``(name, args_sha)``. One signal per streak.
    """
    entries = _entries(tool_calls)
    signals: list[dict] = []
    index = 0
    while index < len(entries):
        streak = 1
        while (
            index + streak < len(entries)
            and _key(entries[index + streak]) == _key(entries[index])
        ):
            streak += 1
        if streak >= 2:
            name, sha = _key(entries[index])
            signals.append(
                signal(
                    SIGNAL_LOOP_FINGERPRINT, "warn", "loop_fingerprint",
                    tool=name, calls=streak, args_sha=sha,
                )
            )
        index += streak
    return signals


def retry_storm_signals(
    tool_calls: list[dict] | None, *, threshold: int = 3
) -> list[dict]:
    """``retry_storm`` warn when >= ``threshold`` calls share ``(name,
    args_sha)`` and at least one of them errored (need not be consecutive).
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for entry in _entries(tool_calls):
        groups.setdefault(_key(entry), []).append(entry)

    signals: list[dict] = []
    for (name, sha), group in groups.items():
        if len(group) < threshold:
            continue
        errors = sum(1 for entry in group if entry.get("status") == "error")
        if errors == 0:
            continue
        signals.append(
            signal(
                SIGNAL_RETRY_STORM, "warn", "retry_storm",
                tool=name, calls=len(group), errors=errors, args_sha=sha,
                threshold=threshold,
            )
        )
    return signals


def duplicate_side_effect_signals(
    tool_calls: list[dict] | None,
    *,
    side_effect_markers: tuple = DEFAULT_SIDE_EFFECT_MARKERS,
) -> list[dict]:
    """``duplicate_side_effect`` FAIL when a side-effecting tool (name contains
    a marker, casefold) ran >= 2x with identical ``(name, args_sha)`` and ALL
    attempts succeeded — the side effect really happened repeatedly.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for entry in _entries(tool_calls):
        groups.setdefault(_key(entry), []).append(entry)

    signals: list[dict] = []
    for (name, sha), group in groups.items():
        if len(group) < 2:
            continue
        if not all(entry.get("status") == "ok" for entry in group):
            continue
        lowered = name.casefold()
        marker = next((m for m in side_effect_markers if m in lowered), None)
        if marker is None:
            continue
        signals.append(
            signal(
                SIGNAL_DUPLICATE_SIDE_EFFECT, "fail", "duplicate_side_effect",
                tool=name, calls=len(group), args_sha=sha, marker=marker,
            )
        )
    return signals


def tool_args_signals(
    tool_calls_raw: str | None,
    tool_calls: list[dict] | None,
    schemas: list[dict],
) -> list[dict]:
    """``tool_args_invalid`` FAIL when a digest entry carries raw ``args`` that
    violate the registered ``tool_schema`` spec for that tool.

    ``schemas`` are CheckRule.spec dicts of kind ``tool_schema``:
    ``{"tool_name": str, "json_schema": dict}``. DOCUMENTED LIMITATION (v1):
    the digest normally carries only ``args_sha``, not the raw args — entries
    without an ``args`` key yield no signal. When the mapper starts shipping
    raw args (forward-compat), validation kicks in with no code change here.
    ``tool_calls_raw`` is accepted for future use (e.g. re-parsing richer
    digests) and unused in v1. Validation uses the minimal type/required/
    properties subset validator shared with output contracts.
    """
    del tool_calls_raw  # v1: digest is authoritative; raw text kept for forward-compat
    if not isinstance(schemas, list):
        return []
    schema_by_tool: dict[str, dict] = {}
    for spec in schemas:
        if not isinstance(spec, dict):
            continue
        tool_name = spec.get("tool_name")
        json_schema = spec.get("json_schema")
        if isinstance(tool_name, str) and isinstance(json_schema, dict):
            schema_by_tool.setdefault(tool_name, json_schema)

    signals: list[dict] = []
    flagged: set[str] = set()
    for entry in _entries(tool_calls):
        if "args" not in entry:
            continue  # digest carries only args_sha: nothing to validate (v1)
        name = entry["name"]
        schema = schema_by_tool.get(name)
        if schema is None or name in flagged:
            continue
        args = entry["args"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue  # unparsable args blob: malformed input, no signal
        try:
            # Lazy import: scoring imports this module for the generalized
            # deterministic override, so a top-level import would be circular.
            # READ-ONLY use of the minimal JSON-schema subset validator.
            from .scoring import validate_json_schema

            valid = validate_json_schema(args, schema)
        except Exception:
            continue  # a broken registered schema must not take analysis down
        if not valid:
            flagged.add(name)
            signals.append(
                signal(
                    SIGNAL_TOOL_ARGS_INVALID, "fail", "tool_args_invalid",
                    tool=name,
                )
            )
    return signals


def _stat_number(stat: object, *attr_names: str) -> float | None:
    for attr in attr_names:
        value = getattr(stat, attr, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def cost_zscore_signals(
    agent_name: str | None,
    *,
    cost: float | None,
    tokens_out: float | None,
    stat: AgentStat | None,
) -> list[dict]:
    """``cost_anomaly`` / ``token_anomaly`` warn when the run's metric sits
    >= 3 sigma above the agent's rolling baseline (``worker.types.AgentStat``).

    A metric fires only with mean+std present, std > 0 and sample_count >= 5 —
    a thin baseline must not accuse anyone. NOTE: AgentStat currently carries
    tokens_out/iterations baselines; the cost baseline attributes
    (``cost_mean``/``cost_usd_mean``) are read via getattr so the signal lights
    up as soon as the stats query grows them.
    """
    del agent_name  # identity is stamped by the caller; kept for call-site clarity
    if stat is None:
        return []
    sample_count = getattr(stat, "sample_count", None)
    if not isinstance(sample_count, int) or sample_count < _MIN_BASELINE_SAMPLES:
        return []

    metrics: list[tuple[str, str, float | None, float | None, float | None]] = [
        (
            SIGNAL_COST_ANOMALY,
            "cost_usd",
            cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            _stat_number(stat, "cost_mean", "cost_usd_mean"),
            _stat_number(stat, "cost_std", "cost_usd_std"),
        ),
        (
            SIGNAL_TOKEN_ANOMALY,
            "tokens_out",
            tokens_out
            if isinstance(tokens_out, (int, float)) and not isinstance(tokens_out, bool)
            else None,
            _stat_number(stat, "tokens_out_mean"),
            _stat_number(stat, "tokens_out_std"),
        ),
    ]

    signals: list[dict] = []
    for signal_name, metric, value, mean, std in metrics:
        if value is None or mean is None or std is None or std <= 0:
            continue
        z = (value - mean) / std
        if z >= _ZSCORE_THRESHOLD:
            signals.append(
                signal(
                    signal_name, "warn", "metric_outlier",
                    metric=metric, value=value, z=z, mean=mean, std=std,
                    sample_count=sample_count,
                )
            )
    return signals


def empty_output_signals(output_text: str | None, tokens_out: int | None) -> list[dict]:
    """``empty_output`` (fail) — the run recorded an output, it was empty, and
    the run's own usage says the model emitted tokens.

    An empty payload has two possible causes and they need different answers:
    the exporter never recorded the output, or the agent genuinely produced
    nothing. Both arrive as "no text to score", and calling both an
    instrumentation defect told the operator to go fix an exporter that was
    working — while the run's most expensive node silently shipped nothing.

    The discriminator is that the field was RECORDED and empty (``""``, not
    absent) while ``gen_ai.usage.output_tokens`` is positive: the exporter
    demonstrably wrote this run's payload and usage, and what it wrote was a
    model that spent its budget and returned no content. That is a fact about
    the agent, not about the instrumentation, so it is a fail-severity signal
    and localises blame here. Absent output (``None``) stays an instrumentation
    warning — nothing was recorded, so nothing can be claimed.

    The score stays UNKNOWN either way: scoring "" produced a hard 0.0, the
    strongest claim from the weakest evidence. This is the deterministic
    channel the empty-payload branch always said the defect belonged in.
    """
    if output_text is None or output_text.strip():
        return []
    if not isinstance(tokens_out, int) or isinstance(tokens_out, bool) or tokens_out <= 0:
        return []
    return [
        signal(
            SIGNAL_EMPTY_OUTPUT, "fail", "empty_output_with_spend",
            tokens_out=tokens_out, chars=len(output_text),
        )
    ]
