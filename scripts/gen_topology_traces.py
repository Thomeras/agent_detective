#!/usr/bin/env python
"""Generate the NON-LINEAR topology corpus: OTLP traces with a known culprit.

Why this exists: ``docs/capabilities.md`` admits under "Honest limits" that the
corpus is "ONE harness, ONE linear topology, ONE injection" — so the graph-first
claim (reconstructing the EXECUTION GRAPH localizes blame better than looking at
nodes in isolation) has never been exercised end-to-end on the shapes it was
built for. The blame engine has unit coverage of fan-out and loops
(``test_fanout_golden``, ``test_loops``, ``test_cycle_localization``), but those
hand-write the edge list. Nothing checked that a real OTLP payload RECONSTRUCTS
into those shapes, which is where the claim can actually break: the mapper, not
the lattice, decides what graph the engine ever sees.

Each cell is one OTLP/HTTP JSON ``ExportTraceServiceRequest``. FOUR of the six
carry an injected contract fault at a named agent, so localization can be scored
against ground truth; the other two are negative controls with NO content or
contract fault at all (do not describe the corpus as "one fault per cell" — two
cells deliberately have none):

============================  =============================================
cell                          shape / injection
============================  =============================================
``fanout_branch_fault``       3 parallel writers + a merge join; ONE writer
                              silently rewrites the carried ``format``
                              parameter (markdown -> html). The joiner does
                              NOT carry ``format`` in its output.
``fanout_join_echoes_breach`` the same fan-out, one byte changed: the joiner
                              ECHOES the rewritten ``format`` it was handed.
                              Two nodes now show an input/output diff on the
                              key; only ONE invented the value. This is the
                              fan-IN case the R2 basis rule has to settle —
                              without the output-side test in
                              ``cutpoint._fresh_contract_origin`` the joiner
                              becomes a second deterministic culprit for a
                              fault it merely merged.
``retry_loop_unrecovered``    draft -> qa -> revise -> draft back-edge (a
                              cycle, see the TIMESTAMP CAVEAT below); the
                              loop's EXIT node rewrites ``format``.
``retry_loop_recovered``      the same cycle, nothing rewritten — a benign
                              retry must not manufacture a defect. NEGATIVE
                              CONTROL: no injected fault.
``retry_runaway``             11 revision attempts chained into one SCC, past
                              ``max_loop_iterations``. The breach is purely
                              STRUCTURAL — no content or contract fault.
``a2a_diamond_fault``         four peers exchanging A2A messages in a diamond
                              (one source, two branches, one editor sink);
                              the translator branch rewrites ``format``.
============================  =============================================

TIMESTAMP CAVEAT — the loop cells' back-edge is NOT causally ordered, and the
tests that use it must not be read as "a retry trace forms a cycle". The
back-edge is a TOOL_DELEGATION span (``handoff_revise``, 1.50-1.60s) naming an
agent whose own span runs LATER (``revise``, 3.10-4.00s); in ``retry_runaway``
the same holds (handoff at 1.2-1.3s naming ``revise_11`` at 11.0-11.5s). The
delegation therefore completes before its target starts. The mapper builds the
edge anyway — rule 2 resolves TOOL_DELEGATION by agent NAME with no temporal
constraint (``otel_mapper/mapper.py``) — so what these cells pin is exactly
that: **the mapper closes a cycle from a name reference**. A causally ordered
version would need the caller's span to still be open when the callee returns,
which makes the CALLER the last-ending member and therefore the SCC's exit node
— a different fixture with different (and, for the deterministic channel,
worse) behaviour. ``test_the_retry_back_edge_is_a_name_reference_not_a_causal
_order`` asserts the contradiction so it can never quietly become a claim about
real retry traces.

Everything is built through the SHIPPED SDK (``detective_sdk.otel``:
``SpanRecord`` / ``to_export_request``), on purpose — if the SDK cannot express
a topology, that is a product finding, and two of them fell out here:

1. ``detective_sdk.tracing`` (the ``run``/``step``/``span`` API) can express
   only chains and trees. A span has ONE ``parentSpanId``, so a fan-IN (three
   workers feeding one joiner) is not expressible at all through it; the join
   edges here are ``TOOL_DELEGATION`` spans, which ``tracing.Span`` has no API
   for (it hard-codes ``openinference.span.kind=AGENT``).
2. A retry loop of the SAME agent cannot form a cycle: the mapper suppresses
   SPAWN between spans of an identical ``gen_ai.agent.name``, and
   TOOL_DELEGATION resolves a target name to the EARLIEST run carrying it. The
   loop cells therefore name each attempt distinctly (``revise_1`` ...), which
   is what an exporter has to do for a loop to be visible as a loop.

Deterministic by construction: fixed trace/span ids and fixed nanosecond
timestamps, so regenerating produces byte-identical files and the corpus can be
diffed. Run::

    .venv/bin/python scripts/gen_topology_traces.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "testdata" / "topologies"
# The manifest is a blame-engine TEST FIXTURE, not corpus data: it holds the
# graph the mapper reconstructed (agent-named nodes, typed edges, topology), so
# the blame-engine suite can exercise the real shapes with nothing but
# ``blame_engine`` importable — that package's suite runs in an environment
# where ``otel_mapper``/``worker`` are not installed.
FIXTURE = REPO / "packages" / "blame_engine" / "tests" / "fixtures" / "topology_corpus.json"

# Import the shipped SDK the same way an integrator would.
sys.path.insert(0, str(REPO / "packages" / "detective_sdk"))
from detective_sdk.otel import SpanRecord, to_export_request  # noqa: E402

AGENT_KIND = "openinference.span.kind"
SERVICE = "topology-corpus"

# 2025-07-08T18:40:00Z. Fixed so the corpus is byte-stable across regenerations.
BASE_NS = 1_752_000_000_000_000_000
SECOND = 1_000_000_000

FORMAT = "markdown"


def _json(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Cell:
    """One trace under construction: deterministic ids, ordered spans."""

    def __init__(self, name: str, trace_prefix: str) -> None:
        self.name = name
        self.trace_id = (trace_prefix * 32)[:32]
        self._next = 1
        self.records: list[SpanRecord] = []

    def _sid(self) -> str:
        span_id = f"{self._next:016x}"
        self._next += 1
        return span_id

    def agent(
        self,
        agent_name: str,
        *,
        parent: str = "",
        input: object = None,
        output: object = None,
        start_s: float,
        end_s: float,
        error: bool = False,
        extra: dict | None = None,
    ) -> str:
        """One AGENT span == one node in the reconstructed graph."""
        span_id = self._sid()
        self.records.append(
            SpanRecord(
                trace_id=self.trace_id,
                span_id=span_id,
                parent_id=parent,
                name=agent_name,
                kind=1,
                start_ns=BASE_NS + int(start_s * SECOND),
                end_ns=BASE_NS + int(end_s * SECOND),
                error=error,
                attributes={
                    AGENT_KIND: "AGENT",
                    "gen_ai.agent.name": agent_name,
                    "input.value": _json(input) if input is not None else "",
                    "output.value": _json(output) if output is not None else "",
                    **(extra or {}),
                },
            )
        )
        return span_id

    def delegation(
        self, tool_name: str, *, parent: str, target: str, start_s: float, end_s: float
    ) -> str:
        """A TOOL span naming a target agent — the mapper's fan-IN edge.

        This is the ONLY way an in-process trace can say "w1's output flowed
        into the joiner": span parenting carries one parent, and the joiner's
        parent is already the orchestrator that spawned it.
        """
        span_id = self._sid()
        self.records.append(
            SpanRecord(
                trace_id=self.trace_id,
                span_id=span_id,
                parent_id=parent,
                name=tool_name,
                kind=1,
                start_ns=BASE_NS + int(start_s * SECOND),
                end_ns=BASE_NS + int(end_s * SECOND),
                attributes={
                    AGENT_KIND: "TOOL",
                    "gen_ai.tool.name": tool_name,
                    "gen_ai.tool.target_agent": target,
                    "input.value": _json({"target": target}),
                },
            )
        )
        return span_id

    def a2a(
        self, name: str, *, parent: str, peer: str, task_id: str, start_s: float, end_s: float
    ) -> str:
        """An A2A client call — ``a2a.task_id`` + ``a2a.peer_agent``.

        Direction (mapper rule 3): a CLIENT span means the peer's response flows
        back to the caller, so the edge points peer -> owner-of-this-span.
        Requires ``a2a_detection=True`` (CLI: ``--a2a``); off by default.
        """
        span_id = self._sid()
        self.records.append(
            SpanRecord(
                trace_id=self.trace_id,
                span_id=span_id,
                parent_id=parent,
                name=name,
                kind=3,  # SPAN_KIND_CLIENT
                start_ns=BASE_NS + int(start_s * SECOND),
                end_ns=BASE_NS + int(end_s * SECOND),
                attributes={
                    "a2a.task_id": task_id,
                    "a2a.peer_agent": peer,
                    "http.request.method": "POST",
                    "url.full": f"https://peers.local/{peer}/tasks/{task_id}",
                },
            )
        )
        return span_id

    def payload(self) -> dict:
        return to_export_request(self.records, service=SERVICE)


# ---------------------------------------------------------------------------
# (a) FAN-OUT: three parallel writers, one joiner, ONE poisoned branch
# ---------------------------------------------------------------------------

_INTRO = {
    "section": "intro",
    "format": FORMAT,
    "text": "## Introduction\n\nThe unit ships in two trims and one carry case.",
}
_SPECS_BAD = {
    "section": "specs",
    "format": "html",  # <-- INJECTION: the carried contract parameter is rewritten
    "text": "<h2>Specs</h2><p>Mass 1.2 kg, runtime 14 hours, two ports.</p>",
}
_PRICING = {
    "section": "pricing",
    "format": FORMAT,
    "text": "## Pricing\n\nA standard tier and an extended-support tier.",
}


def _build_fanout(name: str, trace_prefix: str, *, join_echoes_format: bool) -> Cell:
    """The fan-out cell. ``join_echoes_format`` is the ONE byte that separates the
    two variants: whether the joiner's output still carries the ``format`` key.

    It matters because it decides whether the joiner is even ELIGIBLE for the
    deterministic channel. With the key dropped (the original cell) the joiner
    shows no input/output diff and cannot be a candidate — so a test asserting
    "the lattice does not blame the joiner" there is really asserting a property
    of this payload. With the key echoed, the joiner DOES show a diff on
    ``format`` and eligibility is decided by the R2 basis rule instead. That is
    the honest version of the claim, and the reason both variants exist.
    """
    cell = Cell(name, trace_prefix)
    task = {"goal": "Write a three-section product brief", "format": FORMAT}
    root = cell.agent("orchestrator", input=task, output=None, start_s=0, end_s=9)
    for agent_name, section, out, end in (
        ("write_intro", "intro", _INTRO, 2.0),
        ("write_specs", "specs", _SPECS_BAD, 2.2),
        ("write_pricing", "pricing", _PRICING, 2.4),
    ):
        cell.agent(
            agent_name,
            parent=root,
            input={"section": section, "format": FORMAT, "source": f"datasheet:{section}"},
            output=out,
            start_s=1.0,
            end_s=end,
        )
    document = "\n\n".join([_INTRO["text"], _SPECS_BAD["text"], _PRICING["text"]])
    if join_echoes_format:
        # The joiner is handed the TASK's carried contract (format=markdown) plus
        # the section texts, and reports the format of what it actually assembled
        # — html, because one section arrived that way. Its own input/output diff
        # is therefore indistinguishable from a fresh rewrite: input markdown,
        # output html, no ambiguity to fall back on. The ONLY thing that says it
        # is not the origin is that an ancestor already put html into
        # circulation on this key. That is the R2 basis question, isolated.
        merge_input = {
            "format": FORMAT,
            "sections": [
                {"section": s["section"], "text": s["text"]}
                for s in (_INTRO, _SPECS_BAD, _PRICING)
            ],
        }
        merge_output = {"document": document, "format": _SPECS_BAD["format"]}
    else:
        # The joiner drops the key entirely: no diff, so it is not even ELIGIBLE
        # for the deterministic channel. Nothing about the lattice is proven by
        # its absence from the candidate list here.
        merge_input = {"sections": [_INTRO, _SPECS_BAD, _PRICING]}
        merge_output = {"document": document}
    merge = cell.agent(
        "merge",
        parent=root,
        input=merge_input,
        output=merge_output,
        start_s=3.0,
        end_s=4.0,
    )
    for i, target in enumerate(("write_intro", "write_specs", "write_pricing")):
        cell.delegation(
            f"collect_{target}", parent=merge, target=target,
            start_s=3.1 + i * 0.1, end_s=3.15 + i * 0.1,
        )
    return cell


def build_fanout() -> Cell:
    return _build_fanout("fanout_branch_fault", "a1", join_echoes_format=False)


def build_fanout_join_echo() -> Cell:
    return _build_fanout("fanout_join_echoes_breach", "a2", join_echoes_format=True)


# ---------------------------------------------------------------------------
# (b) RETRY LOOP: draft -> qa -> revise, with revise handing back to draft
# ---------------------------------------------------------------------------


def _build_retry(name: str, trace_prefix: str, *, revise_format: str) -> Cell:
    """The retry cycle. ``revise_format`` is the whole injection knob:
    ``html`` rewrites the carried contract parameter, ``markdown`` preserves it
    (the retry did its job and the run is clean)."""
    cell = Cell(name, trace_prefix)
    task = {"goal": "Draft and revise the launch note", "format": FORMAT}
    root = cell.agent("orchestrator", input=task, output=None, start_s=0, end_s=9)
    draft = cell.agent(
        "draft",
        parent=root,
        input=task,
        output={"format": FORMAT, "text": "# Launch note\n\nFirst pass, thin on detail."},
        start_s=1.0,
        end_s=2.0,
    )
    qa = cell.agent(
        "qa",
        parent=draft,
        input={"format": FORMAT, "text": "# Launch note\n\nFirst pass, thin on detail."},
        output={
            "format": FORMAT,
            "verdict": "reject",
            "notes": "Needs the availability paragraph and a closing line.",
        },
        start_s=2.1,
        end_s=3.0,
    )
    revised_text = (
        "# Launch note\n\nSecond pass with availability and a closing line."
        if revise_format == FORMAT
        else "<h1>Launch note</h1><p>Second pass with availability.</p>"
    )
    cell.agent(
        "revise",
        parent=qa,
        input={
            "format": FORMAT,
            "verdict": "reject",
            "notes": "Needs the availability paragraph and a closing line.",
        },
        output={"format": revise_format, "text": revised_text},
        start_s=3.1,
        end_s=4.0,
    )
    # The loop-back edge. `draft` called `revise`, so revise's output flows back
    # into draft — TOOL_DELEGATION points target -> caller, which closes the cycle
    # draft -> qa -> revise -> draft.
    #
    # NOT CAUSALLY ORDERED, and deliberately left that way (see the TIMESTAMP
    # CAVEAT in the module docstring): this span ends at 1.60s while `revise`
    # runs 3.10-4.00s, so the delegation "returns" 1.5s before its target starts.
    # The mapper resolves the target by NAME with no temporal constraint, which
    # is the fact these cells pin. Making it causal would require draft's span to
    # enclose the cycle, which moves the SCC's exit node onto draft.
    cell.delegation("handoff_revise", parent=draft, target="revise", start_s=1.5, end_s=1.6)
    # The revise span is the loop's EXIT (it finishes last), and publish hangs off
    # it, so the cycle feeds a real sink.
    revise_span = cell.records[3].span_id
    cell.agent(
        "publish",
        parent=revise_span,
        input={"format": revise_format, "text": revised_text},
        output={"document": revised_text, "channel": "blog"},
        start_s=4.1,
        end_s=5.0,
    )
    return cell


def build_retry_unrecovered() -> Cell:
    return _build_retry("retry_loop_unrecovered", "b1", revise_format="html")


def build_retry_recovered() -> Cell:
    return _build_retry("retry_loop_recovered", "b2", revise_format=FORMAT)


def build_retry_runaway() -> Cell:
    """11 revision attempts chained into ONE strongly-connected component.

    Each attempt gets its OWN agent name. That is not cosmetic: the mapper drops
    SPAWN between two spans with the same ``gen_ai.agent.name`` (it reads them as
    nested/retry spans of one agent), so a loop whose attempts share a name has
    no edges between them and can never be an SCC. The limit breach here is
    purely structural — no content fault is injected.
    """
    cell = Cell("retry_runaway", "b3")
    task = {"goal": "Iterate the summary until the reviewer accepts", "format": FORMAT}
    root = cell.agent("orchestrator", input=task, output=None, start_s=0, end_s=30)
    attempts = 11
    parent = root
    first: str | None = None
    for i in range(1, attempts + 1):
        span = cell.agent(
            f"revise_{i}",
            parent=parent,
            input={"format": FORMAT, "attempt": i},
            output={"format": FORMAT, "attempt": i, "text": f"Revision {i} of the summary."},
            start_s=float(i),
            end_s=float(i) + 0.5,
        )
        first = first or span
        parent = span
    # Close the cycle: attempt 1 called the last attempt, so the last attempt's
    # output flows back into it. Same TIMESTAMP CAVEAT as the other loop cells —
    # this handoff ends at 1.3s while `revise_11` runs 11.0-11.5s.
    cell.delegation(
        f"handoff_revise_{attempts}", parent=first or root,
        target=f"revise_{attempts}", start_s=1.2, end_s=1.3,
    )
    return cell


# ---------------------------------------------------------------------------
# (c) A2A: four peers exchanging messages, diamond shape, one bad branch
# ---------------------------------------------------------------------------

_DOCS = {"format": FORMAT, "docs": ["datasheet.md", "faq.md", "changelog.md"]}
_ANALYSIS = {
    "format": FORMAT,
    "analysis": "## Findings\n\nThe changelog and the datasheet agree on the runtime.",
}
_TRANSLATION_BAD = {
    "format": "html",  # <-- INJECTION
    "translation": "<h2>Zjištění</h2><p>Changelog a datasheet se shodují.</p>",
}


def build_a2a() -> Cell:
    cell = Cell("a2a_diamond_fault", "c1")
    task = {"goal": "Localize the release notes", "format": FORMAT}
    # Four PEERS: each is its own top-level AGENT span (no parent), which is what
    # separate processes look like on the wire. All structure comes from A2A.
    retriever = cell.agent(
        "retriever", input=task, output=_DOCS, start_s=0.0, end_s=1.0
    )
    analyst = cell.agent(
        "analyst", input=_DOCS, output=_ANALYSIS, start_s=1.2, end_s=2.0
    )
    cell.a2a(
        "a2a.send", parent=analyst, peer="retriever", task_id="t-001",
        start_s=1.25, end_s=1.30,
    )
    translator = cell.agent(
        "translator", input=_DOCS, output=_TRANSLATION_BAD, start_s=1.2, end_s=2.2
    )
    cell.a2a(
        "a2a.send", parent=translator, peer="retriever", task_id="t-002",
        start_s=1.25, end_s=1.30,
    )
    editor = cell.agent(
        "editor",
        input={"inputs": [_ANALYSIS, _TRANSLATION_BAD]},
        output={"document": _ANALYSIS["analysis"] + "\n\n" + _TRANSLATION_BAD["translation"]},
        start_s=2.4,
        end_s=3.0,
    )
    cell.a2a(
        "a2a.send", parent=editor, peer="analyst", task_id="t-003",
        start_s=2.45, end_s=2.50,
    )
    cell.a2a(
        "a2a.send", parent=editor, peer="translator", task_id="t-004",
        start_s=2.55, end_s=2.60,
    )
    return cell


# ---------------------------------------------------------------------------
# Manifest: the graph the REAL mapper reconstructs from each payload
# ---------------------------------------------------------------------------

# a2a_detection is off by default (build spec 6.1), so the A2A cell declares it.
CELLS: list[tuple[str, callable, bool]] = [
    ("fanout_branch_fault", build_fanout, False),
    ("fanout_join_echoes_breach", build_fanout_join_echo, False),
    ("retry_loop_unrecovered", build_retry_unrecovered, False),
    ("retry_loop_recovered", build_retry_recovered, False),
    ("retry_runaway", build_retry_runaway, False),
    ("a2a_diamond_fault", build_a2a, True),
]


def reconstruct(payload: dict, *, a2a: bool) -> dict:
    """Run the payload through the REAL mapper and describe the graph it built.

    Node identity in the manifest is the AGENT NAME, not the derived run-id
    UUID: the uuid5 of ``<trace>:<span>`` is unreadable and tells a reviewer
    nothing, while the agent name is exactly what ground truth is stated in.
    Names are unique within every cell, so the relabelling is lossless.
    """
    from detective_cli.bundle import bundles_from_exports

    bundles = bundles_from_exports([payload], a2a_detection=a2a)
    if len(bundles) != 1:
        raise SystemExit(f"expected exactly one graph, got {len(bundles)}")
    bundle = bundles[0]
    names = {str(r.run_id): (r.agent_name or str(r.run_id)) for r in bundle.runs}
    nodes = [names[str(r.run_id)] for r in bundle.runs]
    edges = sorted(
        [names[str(e.from_run_id)], names[str(e.to_run_id)], e.type] for e in bundle.edges
    )
    # Relative seconds, rounded: the values only order SCC exit nodes and
    # topological tie-breaks, and datetime round-tripping leaves microsecond
    # noise that would make the manifest look non-deterministic.
    end_times = {
        names[str(r.run_id)]: round(r.ended_at.timestamp() - BASE_NS / 1e9, 3)
        for r in bundle.runs
        if r.ended_at is not None
    }
    from blame_engine.topology import classify_topology

    topology = classify_topology(nodes, [(u, v) for u, v, _t in edges])
    return {
        "nodes": nodes,
        "edges": edges,
        "end_times": end_times,
        "topology": topology,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for name, build, a2a in CELLS:
        cell = build()
        payload = cell.payload()
        path = OUT_DIR / f"{name}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest[name] = {"file": path.name, "a2a": a2a, **reconstruct(payload, a2a=a2a)}
        print(f"{name}: {len(cell.records)} spans -> {path.relative_to(REPO)}")
        print(f"   nodes {manifest[name]['nodes']}")
        print(f"   topology {manifest[name]['topology']['primary']}")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"manifest -> {FIXTURE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
