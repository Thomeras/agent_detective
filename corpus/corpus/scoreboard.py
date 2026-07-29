"""Score the corpus over REPEATED runs and report intervals, not numbers.

    uv run python -m corpus.scoreboard --repeats 5      # needs JUDGE_* configured

Needs a model, so it is NOT a CI step — run by hand, output committed, the same
deal as the traces. What it measures:

- **false positive rate** — of the clean controls, how often a run was reported
  as an incident. This is the number that decides whether the tool can gate a
  build. A detector with a great hit rate and a 50% FPR is one nobody turns on.
- **discrimination** — of the faulted cells, how often the verdict differed from
  that cell's own topology clean baseline. Not "did it report an incident": if
  the clean run of the same topology says the same thing, the tool did not
  notice the fault, it just always says that.

Why repeats. The judged channel is not deterministic: the same trace, the same
prompt and three runs produced discrimination 0.83 / 0.67 / 0.83, and one cell
alternated between `composition_failure` and clean. A single scoreboard run is
therefore not a measurement, and one used to justify a change is worse than
none — it will happily confirm whatever was just done.

Two different uncertainties come out of this and they must not be blended:

- **instability** is measured over RUNS: how often one fixed cell changes its
  own answer. More runs shrink it.
- **sampling error** is over CELLS: 5 control topologies say little about the
  22 that exist. More runs do NOT shrink this one, and the interval below is
  computed over cells for exactly that reason. Reporting a tight interval off
  50 runs of 5 cells would be arithmetic dressed as evidence.

And a third that this tool does NOT measure, so do not read its numbers as if it
did: BETWEEN-SESSION drift. `05_diamond__clean` returned no incident on 5 of 5
runs in one invocation and `composition_failure` on 6 of 6 an hour later, from a
byte-identical trace, unchanged code and an unchanged prompt. Repeats inside one
invocation cannot see that; a verdict that reproduces all afternoon may still not
reproduce next week. It is the strongest argument in this corpus for the
deterministic channel carrying the load a judged one cannot.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from math import sqrt
from pathlib import Path

from detective_cli import analyze, bundles_from_exports, load_trace

_ROOT = Path(__file__).resolve().parents[1]
_TRACES = _ROOT / "traces"


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion.

    Not the textbook normal approximation: at n=4 with 0 successes that returns
    the interval [0, 0], which would state certainty from four observations.
    Wilson stays honest at the edges, which is where this corpus lives.
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _verdict(trace_path: Path) -> dict:
    run = analyze(bundles_from_exports(load_trace(trace_path)))
    graph = run.graphs[0]
    report = graph.blame_report
    if report is None:
        # A graph that came back clean carries no blame report at all — there
        # was nothing to attribute. That is a PASS, not a missing measurement.
        return {"report_type": None, "confidence": 0.0, "incident": False, "culprits": []}
    names = {str(k): v for k, v in graph.agent_names.items()}
    return {
        "report_type": report.get("report_type"),
        "confidence": round(float(report.get("confidence") or 0.0), 3),
        "incident": bool(graph.incident),
        "culprits": sorted(
            names.get(str(r), str(r)) for r in report.get("culprit_run_ids", [])
        ),
    }


def _cells() -> dict[str, dict]:
    out = {}
    for label_path in sorted(_TRACES.glob("*.label.json")):
        trace = label_path.with_name(label_path.name.replace(".label.json", ".json"))
        if trace.exists():
            out[trace.stem] = {"trace": trace, "label": json.loads(label_path.read_text())}
    return out


def build(repeats: int = 3) -> dict:
    cells = _cells()
    runs: dict[str, list[dict]] = defaultdict(list)
    for _ in range(repeats):
        for name, cell in cells.items():
            runs[name].append(_verdict(cell["trace"]))

    controls = {
        n: c
        for n, c in cells.items()
        if c["label"].get("fault") is None and not c["label"].get("expect_incident")
    }
    faulted = {n: c for n, c in cells.items() if c["label"].get("fault") is not None}

    # Per-cell incident rate over runs. A cell that flips is neither a clean
    # pass nor a false positive; it is an unstable measurement, and averaging
    # the rates keeps that visible instead of rounding it away.
    control_rate = {n: sum(v["incident"] for v in runs[n]) / repeats for n in controls}
    fpr = sum(control_rate.values()) / len(controls) if controls else None
    # Interval over CELLS: a control counts as a false positive if it ever fired.
    fp_cells = sum(1 for r in control_rate.values() if r > 0)
    fpr_lo, fpr_hi = wilson(fp_cells, len(controls))

    baseline_types = {
        c["label"]["topology"]: Counter(v["report_type"] for v in runs[n])
        for n, c in controls.items()
    }
    disc_rate = {}
    for name, cell in faulted.items():
        base = baseline_types.get(cell["label"]["topology"])
        if base is None:
            continue
        # Differs from the baseline's MOST COMMON verdict. Comparing against a
        # single baseline run would make discrimination depend on which way that
        # run happened to flip.
        base_type = base.most_common(1)[0][0]
        disc_rate[name] = sum(v["report_type"] != base_type for v in runs[name]) / repeats
    discrimination = sum(disc_rate.values()) / len(disc_rate) if disc_rate else None
    disc_cells = sum(1 for r in disc_rate.values() if r >= 0.5)
    disc_lo, disc_hi = wilson(disc_cells, len(disc_rate))

    # ATTRIBUTION: of the cells whose true origin is known, how often the report
    # names it. This is the product's actual claim — "it names the culprit" —
    # and detection rate says nothing about it. Ground truth is the injected
    # target where there is one, or a hand-verified origin for a fault the
    # topology produced on its own.
    attributed, attribution_rate = {}, {}
    for name, cell in cells.items():
        truth = cell["label"].get("ground_truth_origin") or cell["label"].get("target_agent")
        if not truth:
            continue
        hits = sum(truth in (v["culprits"] or []) for v in runs[name])
        attribution_rate[name] = hits / repeats
        attributed[name] = truth
    attribution = (
        sum(attribution_rate.values()) / len(attribution_rate) if attribution_rate else None
    )
    attr_cells = sum(1 for r in attribution_rate.values() if r >= 0.5)
    attr_lo, attr_hi = wilson(attr_cells, len(attribution_rate))

    unstable = {
        n: dict(Counter(v["report_type"] or "clean" for v in runs[n]))
        for n in cells
        if len({v["report_type"] for v in runs[n]}) > 1
    }

    return {
        "repeats": repeats,
        "cells": {
            n: {
                "label": c["label"],
                "verdicts": [dict(v) for v in runs[n]],
                "report_types": dict(Counter(v["report_type"] or "clean" for v in runs[n])),
            }
            for n, c in cells.items()
        },
        "summary": {
            "cells_total": len(cells),
            "controls": len(controls),
            "false_positive_rate": round(fpr, 3) if fpr is not None else None,
            "false_positive_ci_over_cells": [round(fpr_lo, 3), round(fpr_hi, 3)],
            "faulted": len(disc_rate),
            "discrimination_rate": round(discrimination, 3)
            if discrimination is not None
            else None,
            "discrimination_ci_over_cells": [round(disc_lo, 3), round(disc_hi, 3)],
            "unstable_cells": sorted(unstable),
            "control_incident_rate": {k: round(v, 3) for k, v in sorted(control_rate.items())},
            "blind_cells": sorted(k for k, v in disc_rate.items() if v < 0.5),
            "attribution_cells": len(attribution_rate),
            "attribution_accuracy": round(attribution, 3) if attribution is not None else None,
            "attribution_ci_over_cells": [round(attr_lo, 3), round(attr_hi, 3)],
            "misattributed_cells": {
                k: attributed[k] for k, v in sorted(attribution_rate.items()) if v < 0.5
            },
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    result = build(args.repeats)
    (_ROOT / "scoreboard.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    s = result["summary"]
    print(f"{s['cells_total']} cells x {result['repeats']} runs\n")
    print(
        f"false positive rate  {s['false_positive_rate']}"
        f"   95% CI over {s['controls']} control cells {s['false_positive_ci_over_cells']}"
    )
    print(
        f"discrimination       {s['discrimination_rate']}"
        f"   95% CI over {s['faulted']} faulted cells {s['discrimination_ci_over_cells']}"
    )
    print(
        f"attribution accuracy {s['attribution_accuracy']}"
        f"   95% CI over {s['attribution_cells']} labelled cells {s['attribution_ci_over_cells']}"
    )
    if s["misattributed_cells"]:
        print("\nknown origin the report usually did NOT name:")
        for c, truth in s["misattributed_cells"].items():
            print(f"  {c:54} truth: {truth}")
    if s["unstable_cells"]:
        print(f"\nunstable across runs ({len(s['unstable_cells'])}/{s['cells_total']}):")
        for c in s["unstable_cells"]:
            print(f"  {c:54} {result['cells'][c]['report_types']}")
    if s["blind_cells"]:
        print("\nfaults the verdict usually did not notice:")
        for c in s["blind_cells"]:
            print(f"  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
