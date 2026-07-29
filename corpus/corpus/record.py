"""Record one labelled corpus entry: run a foreign topology, keep the trace.

    uv run python -m corpus.record --topo-db ~/Projekty/agent_topo_db \\
        --topology 21_fan_in_join --fault rewrite_currency --target metrics_analyst

Recording is a DEVELOPER step, not a CI step: it needs agent_topo_db checked out
next door and a live OpenRouter key, and it costs money. What CI consumes is the
result — the trace JSON and its label, both committed under ``corpus/traces/``.
That split is the point. A corpus that has to call a model to be checked is not
a regression suite, it is a bill.

Each entry is recorded TWICE, clean and faulted, from byte-identical topology
code. The clean run is the negative control: without it the suite measures
detection and says nothing about false positives, which is the number that
decides whether anyone can put this in front of a build.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.trace import SpanKind

from .inject import FAULTS, Fault, ResponseInjector
from .otel_bridge import build_tracer_provider
from .topolab_adapter import TopolabTracer, install_usage_capture


def _load_topology_module(topo_db: Path, topology: str):
    """Load ``topologies/<name>/main.py`` the way agent_topo_db's own runner does."""
    main_py = topo_db / "topologies" / topology / "main.py"
    if not main_py.is_file():
        raise SystemExit(f"no such topology: {main_py}")
    spec = importlib.util.spec_from_file_location(f"topo_{topology}", main_py)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {main_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(
    topo_db: Path,
    topology: str,
    out_dir: Path,
    fault_name: str | None = None,
    target_agent: str | None = None,
) -> Path:
    sys.path.insert(0, str(topo_db / "src"))

    label = "clean" if fault_name is None else f"{fault_name}@{target_agent}"
    slug = f"{topology}__{label.replace('@', '_at_')}"
    trace_path = out_dir / f"{slug}.json"

    fault = None
    if fault_name is not None:
        if fault_name not in FAULTS:
            raise SystemExit(f"unknown fault {fault_name!r}; have {sorted(FAULTS)}")
        if not target_agent:
            raise SystemExit("--fault needs --target <agent name>")
        fault = Fault(name=fault_name, target_agent=target_agent, apply=FAULTS[fault_name])
    injector = ResponseInjector(fault)

    provider, exporter = build_tracer_provider(
        trace_path, service_name=f"agent-topo-db/{topology}"
    )
    tracer = provider.get_tracer("agent-detective-corpus")

    module = _load_topology_module(topo_db, topology)
    topology_main = module.main
    # The ORIGINAL request, read off the topology itself. This is the provenance
    # the terminal judge cites — it is the only thing it can check the
    # deliverable against. Naming the topology here instead (which this recorder
    # did at first) hands the judge a string that is not a request at all, and
    # it then correctly reports that the output does not answer it: a false
    # incident manufactured entirely by the harness.
    task = getattr(module, "TASK", None)
    if not task:
        raise SystemExit(
            f"{topology} exposes no TASK constant; the terminal judge would have "
            f"nothing to check the deliverable against. No entry written."
        )

    root = tracer.start_span(topology, kind=SpanKind.INTERNAL)
    root.set_attribute("openinference.span.kind", "AGENT")
    root.set_attribute("gen_ai.agent.name", topology)
    root.set_attribute("input.value", task)
    root.set_attribute("output.value", "")

    install_usage_capture()
    adapter = TopolabTracer(tracer, root, injector=injector)
    adapter.install()
    try:
        topology_main()
    except BaseException:
        # The exporter flushes on shutdown, so a crashed run would otherwise
        # leave a truncated trace with no label sitting in the corpus —
        # indistinguishable from a real entry, and wrong. Take it back out.
        adapter.uninstall()
        root.end()
        provider.shutdown()
        trace_path.unlink(missing_ok=True)
        raise
    adapter.uninstall()
    root.end()
    provider.shutdown()

    if fault is not None and injector.applied == 0:
        # A faulted entry whose fault never fired is a clean run wearing a label
        # that says otherwise — the single worst thing that can enter a corpus.
        trace_path.unlink(missing_ok=True)
        ran = injector.no_ops > 0
        raise SystemExit(
            f"fault {fault_name!r} on {target_agent!r} changed nothing"
            + (
                f" ({injector.no_ops} invocation(s) matched no text)"
                if ran
                else f"; that agent never ran in {topology}"
            )
            + ". No entry written."
        )

    meta = {
        "topology": topology,
        "task": task,
        "source": "agent_topo_db",
        "instrumentation": "opentelemetry-sdk + corpus.topolab_adapter (foreign)",
        "fault": fault_name,
        "target_agent": target_agent,
        "injections_applied": injector.applied,
        "expect_incident": fault is not None,
        # Faults whose consequence is a NAMED deterministic signal carry that
        # expectation into the label, so CI can assert ground truth with no
        # model in the path. Faults that only degrade prose cannot: their
        # detection is a judged question and belongs in the scoreboard.
        **(
            {"expect_signal": "empty_output", "expect_origin": target_agent}
            if fault_name == "empty_answer"
            else {}
        ),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / f"{slug}.label.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return trace_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--topo-db", type=Path, required=True)
    ap.add_argument("--topology", required=True)
    ap.add_argument("--fault", default=None, choices=sorted(FAULTS))
    ap.add_argument("--target", default=None, help="agent whose output gets the fault")
    ap.add_argument(
        "--out", type=Path, default=Path(__file__).resolve().parents[1] / "traces"
    )
    args = ap.parse_args()

    path = record(
        args.topo_db.expanduser(), args.topology, args.out, args.fault, args.target
    )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
