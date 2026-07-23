"""Golden replay for blame reports: fixture loading, stable surface, diffing.

detective-ci replays a recorded blame fixture through the pure ``blame_engine``
(no LLM, no I/O beyond the two JSON files) and compares the result against a
golden snapshot on the STABLE surface only.

Fixture JSON schema (``load_fixture``)
--------------------------------------

::

    {
      "nodes": ["run_id", ...],                     # required
      "edges": [["from_run_id", "to_run_id"], ...], # required (cycles allowed)
      "scores": {                                    # required; per run_id:
        "<run_id>": {
          "score": 0.15 | null,                     # null = UNKNOWN
          "flags": ["missing_required_content", ...],          # optional
          "contract_violations": [["file_type", "docx", "md"]],# optional
          "deterministic_signals": [{"name": ..., "severity":  # optional
              "fail"|"warn", "detail": ..., "basis": ...}],
          "unscored_reason": "payload_missing" | null,         # optional
          "input_flawed": true | false | null,                 # optional
          "components": {"judge": 0.2},                        # optional
          "judge_note": "..."                                  # optional
        }
      },
      "agent_names": {"<run_id>": "agent_name"},    # required
      "terminal_verdict": {                          # optional
        "bad": false, "score": 1.0, "reasoning": "...",
        "checkable": true,                          # optional, default true
        "stale": false                              # optional, default false
      },
      "config": {"threshold": 0.5, ...},            # optional BlameConfig kwargs
      "node_costs": {"<run_id>": 1.0},              # optional, default 1.0
      "node_end_times": {"<run_id>": 3.0},          # optional, default node order
      "error_span_ids": {"<run_id>": ["span", ...]} # optional, default {}
    }

Runs missing from ``scores`` load as unscored (score ``None``,
``unscored_reason="payload_missing"``) — same convention as the blame_engine
test factory, so a structural root can simply be omitted.

The stable surface
------------------

``stable_surface`` reduces a :class:`blame_engine.BlameReport` to exactly what
is reproducible run over run:

- ``report_type``
- ``culprit_agents`` — sorted agent names of the culprit runs
- ``deterministic_signals`` — sorted set of fired deterministic signal names;
  a recorded input-contract breach (``evidence.contract_violations``) counts as
  the precedent signal name ``contract_violation``
- ``flags`` — sorted union of tier1-style per-node flags, present only when
  the fixture carried any

Confidences are DELIBERATELY absent: LLM judge scores are not reproducible to
the third decimal, deterministic outputs are — which is exactly why they (and
only they) anchor the snapshot.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from pathlib import Path

from blame_engine import (
    BlameConfig,
    BlameInput,
    BlameReport,
    NodeScore,
    TerminalVerdict,
    find_blame,
)

_TOP_LEVEL_KEYS = {
    "nodes",
    "edges",
    "scores",
    "agent_names",
    "terminal_verdict",
    "config",
    "node_costs",
    "node_end_times",
    "error_span_ids",
}

_SCORE_KEYS = {
    "score",
    "flags",
    "contract_violations",
    "deterministic_signals",
    "unscored_reason",
    "input_flawed",
    "components",
    "judge_note",
}


def _node_score(run_id: str, raw: Mapping | None) -> NodeScore:
    if raw is None:
        # Absent from "scores" = never scored (e.g. a structural root).
        return NodeScore(
            run_id=run_id,
            score=None,
            components={},
            input_flawed=None,
            unscored_reason="payload_missing",
            judge_note=None,
        )
    unknown = set(raw) - _SCORE_KEYS
    if unknown:
        raise ValueError(
            f"fixture scores[{run_id!r}] has unknown keys: {sorted(unknown)}"
        )
    score = raw.get("score")
    return NodeScore(
        run_id=run_id,
        score=score,
        components=dict(raw.get("components", {})),
        input_flawed=raw.get("input_flawed"),
        unscored_reason=raw.get(
            "unscored_reason", None if score is not None else "payload_missing"
        ),
        judge_note=raw.get("judge_note"),
        flags=tuple(raw.get("flags", ())),
        contract_violations=tuple(
            tuple(v) for v in raw.get("contract_violations", ())
        ),
        deterministic_signals=tuple(
            dict(s) for s in raw.get("deterministic_signals", ())
        ),
    )


def load_fixture(path: str | Path) -> BlameInput:
    """Load a fixture JSON file (schema in the module docstring) as BlameInput."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"fixture {path} has unknown top-level keys: {sorted(unknown)}")
    for required in ("nodes", "edges", "scores", "agent_names"):
        if required not in data:
            raise ValueError(f"fixture {path} is missing required key {required!r}")

    nodes: list[str] = list(data["nodes"])
    raw_scores: Mapping = data["scores"]
    tv = data.get("terminal_verdict")
    terminal = None
    if tv is not None:
        terminal = TerminalVerdict(
            bad=tv["bad"],
            score=tv.get("score"),
            reasoning=tv.get("reasoning"),
            checkable=tv.get("checkable", True),
            stale=tv.get("stale", False),
        )
    costs = data.get("node_costs", {})
    end_times = data.get("node_end_times", {})
    return BlameInput(
        nodes=nodes,
        edges=[tuple(e) for e in data["edges"]],
        scores={n: _node_score(n, raw_scores.get(n)) for n in nodes},
        node_costs={n: float(costs.get(n, 1.0)) for n in nodes},
        node_end_times={
            n: float(end_times.get(n, float(i))) for i, n in enumerate(nodes)
        },
        agent_names={n: data["agent_names"].get(n, n) for n in nodes},
        error_span_ids={
            k: list(v) for k, v in data.get("error_span_ids", {}).items()
        },
        terminal_verdict=terminal,
        loop_baselines={},
        config=BlameConfig(**data["config"]) if data.get("config") else BlameConfig(),
    )


def stable_surface(
    report: BlameReport, agent_names: Mapping[str, str] | None = None
) -> dict:
    """Reduce a BlameReport to its reproducible surface (module docstring).

    ``agent_names`` maps run_id -> agent name for the culprit list; when omitted
    the run_ids themselves are used (the blame_engine test-factory convention).
    NO confidence value ever appears here — judge-derived numbers are not
    reproducible, so they must not gate CI.
    """
    names = agent_names or {}
    signal_names = {s["name"] for s in report.evidence.deterministic_signals}
    if report.evidence.contract_violations:
        # The precedent deterministic signal (docs/deterministic-signals.md):
        # a recorded input-contract breach is reproducible check output.
        signal_names.add("contract_violation")
    surface: dict = {
        "report_type": report.report_type,
        "culprit_agents": sorted(
            {names.get(r, r) for r in report.culprit_run_ids}
        ),
        "deterministic_signals": sorted(signal_names),
    }
    flags = sorted(
        {f for fl in report.evidence.node_flags.values() for f in fl}
    )
    if flags:  # tier1-style flags, only when the fixture carried any
        surface["flags"] = flags
    return surface


def _surface_for(fixture_path: str | Path) -> dict:
    inp = load_fixture(fixture_path)
    return stable_surface(find_blame(inp), agent_names=inp.agent_names)


def _dumps(surface: dict) -> str:
    return json.dumps(surface, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def record(fixture_path: str | Path, golden_path: str | Path) -> dict:
    """Replay the fixture and write its stable surface as sorted-keys JSON."""
    surface = _surface_for(fixture_path)
    Path(golden_path).write_text(_dumps(surface), encoding="utf-8")
    return surface


def assert_matches_golden(
    fixture_path: str | Path, golden_path: str | Path
) -> None:
    """Replay the fixture; raise AssertionError with a unified diff on mismatch."""
    current = _surface_for(fixture_path)
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    if current == golden:
        return
    diff = "\n".join(
        difflib.unified_diff(
            _dumps(golden).splitlines(),
            _dumps(current).splitlines(),
            fromfile=f"golden: {golden_path}",
            tofile=f"current: {fixture_path}",
            lineterm="",
        )
    )
    raise AssertionError(
        "blame surface regressed against the golden snapshot "
        "(stable surface only — confidences are never compared):\n" + diff
    )
