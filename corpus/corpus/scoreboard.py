"""Score the whole corpus and write down the two numbers that matter.

    uv run python -m corpus.scoreboard          # needs JUDGE_* configured

Needs a model, so it is NOT a CI step — it is run by hand and its output is
committed, the same deal as the traces themselves. What it produces:

- **false positive rate** — of the clean cells, how many were reported as an
  incident. This is the number that decides whether the tool can gate a build.
  A detector with a great hit rate and a 50% FPR is a detector nobody turns on.
- **discrimination** — of the faulted cells, how many produced a verdict that
  differs from their own topology's clean baseline. Not "did it report an
  incident": if the clean run of the same topology reports the same thing, the
  tool did not notice the fault, it just always says that.

Discrimination is deliberately measured against the paired baseline rather than
against an absolute expectation. A verdict that fires on everything is worth
nothing, and only the pairing makes that visible.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from detective_cli import analyze, bundles_from_exports, load_trace

_ROOT = Path(__file__).resolve().parents[1]
_TRACES = _ROOT / "traces"


def _verdict(trace_path: Path) -> dict:
    run = analyze(bundles_from_exports(load_trace(trace_path)))
    graph = run.graphs[0]
    report = graph.blame_report
    names = {str(k): v for k, v in graph.agent_names.items()}
    return {
        "report_type": report.get("report_type"),
        "confidence": round(float(report.get("confidence") or 0.0), 3),
        "incident": bool(graph.incident),
        "culprits": sorted(
            names.get(str(r), str(r)) for r in report.get("culprit_run_ids", [])
        ),
    }


def build() -> dict:
    cells = {}
    for label_path in sorted(_TRACES.glob("*.label.json")):
        trace = label_path.with_name(label_path.name.replace(".label.json", ".json"))
        if not trace.exists():
            continue
        label = json.loads(label_path.read_text())
        cells[trace.stem] = {"label": label, "verdict": _verdict(trace)}

    clean = {k: v for k, v in cells.items() if v["label"].get("fault") is None}
    faulted = {k: v for k, v in cells.items() if v["label"].get("fault") is not None}

    # Only cells whose label says the run itself was sound count as controls; a
    # baseline that legitimately broke (see the empty_output cell) is not a
    # false positive, it is a true one.
    controls = {k: v for k, v in clean.items() if not v["label"].get("expect_incident")}
    false_positives = [k for k, v in controls.items() if v["verdict"]["incident"]]

    baseline_by_topology = {
        v["label"]["topology"]: v["verdict"] for k, v in controls.items()
    }
    discriminated, blind = [], []
    for name, cell in faulted.items():
        base = baseline_by_topology.get(cell["label"]["topology"])
        same = base is not None and (
            base["report_type"] == cell["verdict"]["report_type"]
            and base["confidence"] == cell["verdict"]["confidence"]
        )
        (blind if same else discriminated).append(name)

    return {
        "cells": cells,
        "summary": {
            "cells_total": len(cells),
            "controls": len(controls),
            "false_positives": len(false_positives),
            "false_positive_rate": round(len(false_positives) / len(controls), 3)
            if controls
            else None,
            "faulted": len(faulted),
            "discriminated": len(discriminated),
            "discrimination_rate": round(len(discriminated) / len(faulted), 3)
            if faulted
            else None,
            "false_positive_cells": sorted(false_positives),
            "blind_cells": sorted(blind),
        },
    }


def main() -> int:
    result = build()
    (_ROOT / "scoreboard.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    s = result["summary"]
    print(f"cells                {s['cells_total']}")
    print(f"false positive rate  {s['false_positive_rate']}  ({s['false_positives']}/{s['controls']} controls)")
    print(f"discrimination       {s['discrimination_rate']}  ({s['discriminated']}/{s['faulted']} faulted)")
    if s["blind_cells"]:
        print("\nfaults the verdict did not notice (identical to their own baseline):")
        for c in s["blind_cells"]:
            print(f"  {c}")
    by_type: dict[str, int] = defaultdict(int)
    for cell in result["cells"].values():
        by_type[cell["verdict"]["report_type"]] += 1
    print("\nverdict distribution: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
