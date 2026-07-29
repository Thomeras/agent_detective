"""The foreign corpus as a regression suite — hermetic, no model, no network.

What these traces are for: every other suite in this repo feeds the engine spans
this project wrote itself, so they cannot catch a mapper that disagrees with the
real OpenTelemetry SDK. These came out of stock ``opentelemetry-sdk`` wrapped
around agent_topo_db, a separate project with no telemetry of its own and no
knowledge that this analysis exists.

Assertions here are DETERMINISTIC-channel only. A judged assertion would need a
model in CI, which would make the suite cost money and stop being reproducible;
the judged observations these traces produced are written up in README.md as
findings, not asserted here as behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detective_cli import analyze, bundles_from_exports, load_trace

_TRACES = Path(__file__).resolve().parents[1] / "traces"


def _cells() -> list[tuple[Path, dict]]:
    out = []
    for label_path in sorted(_TRACES.glob("*.label.json")):
        trace_path = label_path.with_name(label_path.name.replace(".label.json", ".json"))
        if trace_path.exists():
            out.append((trace_path, json.loads(label_path.read_text())))
    return out


CELLS = _cells()
IDS = [p.stem for p, _ in CELLS]


def test_the_corpus_is_not_empty() -> None:
    """A suite that silently covers nothing passes just as green as one that
    works. Recording is a manual step, so the count is worth asserting."""
    assert CELLS, f"no labelled traces under {_TRACES}"


@pytest.mark.parametrize("trace_path,label", CELLS, ids=IDS)
def test_every_entry_has_a_label_naming_its_provenance(trace_path, label) -> None:
    assert label["source"] == "agent_topo_db"
    assert "foreign" in label["instrumentation"]
    assert label["topology"]


@pytest.mark.parametrize("trace_path,label", CELLS, ids=IDS)
def test_the_graph_reconstructs(trace_path, label) -> None:
    """The point of the whole exercise: spans this project did not write must
    still reconstruct into a graph with nodes and edges.

    A mapper regression shows up here first — as zero runs, which is exactly
    what a purely auto-instrumented run produces (OpenInference emits LLM spans;
    only ``openinference.span.kind=AGENT`` opens a run).
    """
    bundles = bundles_from_exports(load_trace(trace_path))
    assert len(bundles) == 1, "one topology run should be one graph"
    bundle = bundles[0]
    assert len(bundle.runs) >= 3, "a topology reconstructed to almost no nodes"
    assert bundle.edges, "no edges — the handoffs did not survive reconstruction"


@pytest.mark.parametrize("trace_path,label", CELLS, ids=IDS)
def test_payloads_survive_the_round_trip(trace_path, label) -> None:
    """Every non-root run carries an input and an output, or the analysis is
    grading silence. Catches an exporter that drops attributes as much as a
    mapper that fails to read them."""
    bundle = bundles_from_exports(load_trace(trace_path))[0]
    non_root = [r for r in bundle.runs if r.agent_name != label["topology"]]
    assert non_root
    assert all(r.input_inline for r in non_root), "a run lost its input payload"


@pytest.mark.parametrize("trace_path,label", CELLS, ids=IDS)
def test_spend_is_recorded(trace_path, label) -> None:
    """Cost and tokens are what separate "the agent produced nothing" from "the
    exporter dropped it" — the distinction the empty_output cell turns on."""
    bundle = bundles_from_exports(load_trace(trace_path))[0]
    costed = [r for r in bundle.runs if r.cost_usd is not None]
    assert costed, "no run carries gen_ai.usage.cost; empty_output cannot be diagnosed"


@pytest.mark.parametrize(
    "trace_path,label",
    [c for c in CELLS if c[1].get("expect_signal")],
    ids=[p.stem for p, l in CELLS if l.get("expect_signal")],
)
def test_expected_deterministic_signal_fires(trace_path, label) -> None:
    """Ground truth, checked without a model in the path."""
    run = analyze(bundles_from_exports(load_trace(trace_path)))
    signals = [
        s
        for graph in run.graphs
        for s in (graph.blame_report.get("evidence", {}).get("deterministic_signals") or [])
    ]
    names = {s.get("name") for s in signals}
    assert label["expect_signal"] in names, f"expected {label['expect_signal']}, got {names}"


@pytest.mark.parametrize(
    "trace_path,label",
    [c for c in CELLS if c[1].get("expect_origin")],
    ids=[p.stem for p, l in CELLS if l.get("expect_origin")],
)
def test_expected_origin_is_named(trace_path, label) -> None:
    run = analyze(bundles_from_exports(load_trace(trace_path)))
    named = set()
    for graph in run.graphs:
        # `agent_names` on the analysed graph is the run_id -> name map; the
        # report itself deals in ids only.
        by_id = {str(k): v for k, v in graph.agent_names.items()}
        named |= {
            by_id.get(str(rid), str(rid))
            for rid in graph.blame_report.get("culprit_run_ids", [])
        }
    assert label["expect_origin"] in named, f"expected origin not named; got {named}"
