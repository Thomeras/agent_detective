"""The two shapes span nesting cannot express: a fan-IN and a loop.

Both were product findings, not hypotheses. Building the non-linear trace corpus
through this SDK hit them and had to route around them by hand-writing OTLP:

1. A span carries ONE ``parentSpanId``. Three workers and the joiner that merged
   them therefore reconstruct as four siblings of the orchestrator, with NO
   worker -> join edge at all. Blame stops at the join and never reaches the arm
   that poisoned it.
2. Three attempts of one agent written with ``step`` reconstruct as a chain
   (``scc_count`` 0, classified ``pipeline``) — or, when the attempts share an
   agent name, as disconnected nodes with no edge between them at all. The
   engine's loop machinery can never fire on that.

Fixed by ``branch``/``join`` and ``retry``/``attempt``. EVERY claim here is
checked by RECONSTRUCTION: the payload goes through the real ``otel_mapper``
(``flatten_export_request`` -> ``map_spans``) and the assertions are on the runs
and edges that come back. Asserting on the JSON this SDK just wrote would prove
only that it wrote what it wrote — the whole failure mode is that plausible
attributes reconstruct into the wrong graph.

``otel_mapper`` is dependency-free, so when it is not installed (each package's
suite runs in its own environment in CI) it is imported from the repo checkout
rather than skipped. ``blame_engine`` needs networkx and is genuinely optional:
the tests that consult the shipped topology classifier skip without it, and the
edge-level assertions that carry the proof do not depend on it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from detective_sdk import run

_PACKAGES = Path(__file__).resolve().parents[2]

if importlib.util.find_spec("otel_mapper") is None and (_PACKAGES / "otel_mapper").is_dir():
    sys.path.insert(0, str(_PACKAGES / "otel_mapper"))
_mapper = pytest.importorskip("otel_mapper.mapper", reason="otel_mapper is not importable")

if importlib.util.find_spec("blame_engine") is None and (_PACKAGES / "blame_engine").is_dir():
    sys.path.insert(0, str(_PACKAGES / "blame_engine"))
try:  # networkx is a real dependency of the classifier; the SDK env has none
    from blame_engine.topology import classify_topology
except Exception:  # noqa: BLE001
    classify_topology = None

needs_engine = pytest.mark.skipif(
    classify_topology is None, reason="blame_engine/networkx not importable here"
)


class Graph:
    """What the mapper reconstructed, addressed by agent name."""

    def __init__(self, runs, edges, labels) -> None:
        self.runs = runs            # agent label -> AgentRunCandidate
        self.edges = edges          # {(from label, to label)}
        self.typed = labels         # {(from label, to label, edge type)}

    def predecessors(self, node: str) -> set[str]:
        return {u for u, v in self.edges if v == node}

    def has_cycle_through(self, *path: str) -> bool:
        """Every consecutive pair is an edge, and the path returns to its start."""
        ring = list(path) + [path[0]]
        return all((u, v) in self.edges for u, v in zip(ring, ring[1:]))


def _graph(*runs) -> Graph:
    """Reconstruct one or more exported runs through the real mapper.

    Node identity is the RUN, never the agent name: a repeated name gets a
    ``~2`` suffix instead of being merged. Merging is not cosmetic — collapsing
    three attempts of one agent into a single node manufactures a cycle out of a
    chain, which is exactly the false positive these tests exist to rule out.
    """
    spans: list[dict] = []
    for r in runs:
        spans.extend(_mapper.flatten_export_request(r.build_payload()))
    result = _mapper.map_spans(spans)
    label: dict[str, str] = {}
    seen: dict[str, int] = {}
    ordered = sorted(result.runs, key=lambda c: (c.start_time, c.trace_id, c.run_key))
    for candidate in ordered:
        name = candidate.agent_name or candidate.run_key
        seen[name] = seen.get(name, 0) + 1
        label[candidate.run_key] = name if seen[name] == 1 else f"{name}~{seen[name]}"
    return Graph(
        runs={label[c.run_key]: c for c in ordered},
        edges={(label[e.from_run_key], label[e.to_run_key]) for e in result.edges},
        labels={
            (label[e.from_run_key], label[e.to_run_key], e.type.value) for e in result.edges
        },
    )


def _enabled(tmp_path, name="orchestrator", **kw):
    return run(name, trace_file=str(tmp_path / "trace.json"), **kw)


def _fan_out_run(tmp_path):
    """plan -> three parallel writers -> merge -> publish, via branch/join."""
    r = _enabled(tmp_path, task={"goal": "product brief", "format": "markdown"})
    with r.step("plan") as p:
        p.output = {"sections": ["intro", "specs", "pricing"]}
    arms = []
    for section in ("intro", "specs", "pricing"):
        with r.branch(f"write_{section}") as w:
            w.output = {"section": section, "text": f"## {section}"}
            arms.append(w)
    with r.join("merge", arms) as m:
        m.output = {"document": "## intro\n## specs\n## pricing"}
    with r.step("publish") as pub:
        pub.output = {"url": "https://example/brief"}
    return r


def _retry_run(tmp_path, attempts=3):
    """A write/qa retry loop under one controller, via retry/attempt."""
    r = _enabled(tmp_path, "editor", task={"goal": "launch note"})
    with r.retry("revise_loop") as loop:
        for i in range(attempts):
            with loop.attempt("write") as a:
                a.output = {"draft": i}
            with loop.attempt("qa") as a:
                a.output = {"verdict": "accept" if i == attempts - 1 else "reject"}
        loop.output = {"draft": attempts - 1}
    return r


def _tool_spans(r) -> list[dict]:
    out = []
    for span in r.build_payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        attrs = {a["key"]: a["value"]["stringValue"] for a in span["attributes"]}
        if attrs.get("openinference.span.kind") == "TOOL":
            out.append({**span, "attrs": attrs})
    return out


def _agent_spans(r) -> dict[str, dict]:
    out = {}
    for span in r.build_payload()["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        attrs = {a["key"]: a["value"]["stringValue"] for a in span["attributes"]}
        if attrs.get("openinference.span.kind") == "AGENT":
            out[attrs["gen_ai.agent.name"]] = {**span, "attrs": attrs}
    return out


class TestFanInWasUnreachable:
    """The failure `join` exists for — pinned so it cannot come back unnoticed."""

    def test_span_nesting_leaves_the_joiner_with_no_link_to_the_work_it_merged(
        self, tmp_path
    ):
        # Three workers and a joiner written the only way the SDK used to allow:
        # four siblings. The joiner's single predecessor is the orchestrator
        # wrapper, so blame walking back from a bad merge finds no arm at all.
        r = _enabled(tmp_path, task={"goal": "brief"})
        for name in ("w1", "w2", "w3"):
            with r.span(name) as s:
                s.output = {"part": name}
        with r.span("join") as j:
            j.output = {"document": "..."}
        graph = _graph(r)
        assert graph.predecessors("join") == {"orchestrator"}
        assert not {e for e in graph.edges if e[0].startswith("w") and e[1] == "join"}


class TestFanInReconstructs:
    def test_every_arm_gets_an_edge_into_the_joiner(self, tmp_path):
        # THE fix: the joiner's predecessors are the three arms it merged (plus
        # the step that dispatched them), so blame can walk back into a branch.
        graph = _graph(_fan_out_run(tmp_path))
        assert graph.predecessors("merge") == {
            "plan",
            "write_intro",
            "write_specs",
            "write_pricing",
        }
        assert ("write_specs", "merge", "TOOL_DELEGATION") in graph.typed

    def test_the_arms_are_parallel_not_a_chain(self, tmp_path):
        # `step` would have chained arm 2 behind arm 1 — a pipeline that never
        # ran, and a handoff comparison between two agents that never spoke.
        graph = _graph(_fan_out_run(tmp_path))
        arms = ["write_intro", "write_specs", "write_pricing"]
        assert all(graph.predecessors(arm) == {"plan"} for arm in arms)
        assert not {(u, v) for u, v in graph.edges if u in arms and v in arms}

    def test_arms_that_run_at_the_same_time_still_do_not_chain(self, tmp_path):
        # Real parallel work is open all at once (three threads, three running
        # arms). Taking "the innermost open span" as the parent would hang arm 2
        # off arm 1 and draw a pipeline nobody ran.
        r = _enabled(tmp_path, task={"goal": "brief"})
        with r.step("plan") as p:
            p.output = {"sections": 3}
        arms = [r.branch(f"w{i}") for i in range(3)]
        for i, arm in enumerate(arms):
            arm.end({"part": i})
        with r.join("merge", arms) as m:
            m.output = {"document": "..."}
        graph = _graph(r)
        assert all(graph.predecessors(f"w{i}") == {"plan"} for i in range(3))
        assert graph.predecessors("merge") == {"plan", "w0", "w1", "w2"}

    def test_a_nested_fan_out_names_the_arm_it_belongs_to(self, tmp_path):
        # `of=` is the escape for a second fan-out inside an arm that is still
        # running — the one case the sibling rule cannot infer.
        r = _enabled(tmp_path, task={"goal": "brief"})
        outer = r.branch("research")
        inner = [r.branch(f"lookup{i}", of=outer) for i in range(2)]
        for i, arm in enumerate(inner):
            arm.end({"hit": i})
        outer.end({"found": 2})
        graph = _graph(r)
        assert graph.predecessors("lookup0") == {"research"}
        assert graph.predecessors("lookup1") == {"research"}

    def test_the_pipeline_resumes_at_the_join(self, tmp_path):
        # A step after a fan-in continues from the merge, not from whichever arm
        # happened to be created last.
        graph = _graph(_fan_out_run(tmp_path))
        assert graph.predecessors("publish") == {"merge"}

    def test_the_joiner_input_carries_what_each_arm_produced(self, tmp_path):
        # Without it the merge looks like it invented its result, and the
        # judge has nothing to compare the document against.
        graph = _graph(_fan_out_run(tmp_path))
        merged = json.loads(graph.runs["merge"].input)
        assert set(merged) == {"write_intro", "write_specs", "write_pricing"}
        assert merged["write_specs"]["text"] == "## specs"

    def test_an_arm_with_no_recorded_output_is_omitted_not_nulled(self, tmp_path):
        # Absence of evidence must not become a claim: entering null for an arm
        # whose output the SDK never saw would read as "that arm produced
        # nothing", which is a different statement from "not recorded here".
        r = _enabled(tmp_path, task={"goal": "brief"})
        with r.branch("w1") as a:
            a.output = {"part": 1}
        with r.branch("w2") as b:
            pass  # never set an output
        with r.join("merge", [a, b, "remote_worker"]) as m:
            m.output = {"document": "..."}
        merged = json.loads(_agent_spans(r)["merge"]["attrs"]["input.value"])
        assert merged == {"w1": {"part": 1}}

    def test_a_join_with_nothing_observed_records_no_input_rather_than_empty(
        self, tmp_path
    ):
        # `{}` would claim the joiner was handed an empty collection.
        r = _enabled(tmp_path, task=None)
        with r.branch("w1"):
            pass
        with r.join("merge", ["remote_a", "remote_b"]) as m:
            m.output = {"document": "..."}
        assert _agent_spans(r)["merge"]["attrs"]["input.value"] == ""

    def test_a_peer_in_another_process_joins_by_name(self, tmp_path):
        # A distributed fan-in: the joiner never holds the peer's Span object,
        # only its agent name. The edge appears once both runs reach the mapper
        # in the same batch.
        producer = _enabled(tmp_path, "collector", task={"goal": "gather"})
        with producer.step("fetch_filings") as s:
            s.output = {"docs": 3}
        consumer = run("writer", task={"goal": "write"}, trace_file=str(tmp_path / "b.json"))
        with consumer.join("summarize", ["fetch_filings"], input={"docs": 3}) as j:
            j.output = {"summary": "..."}
        graph = _graph(producer, consumer)
        assert ("fetch_filings", "summarize", "TOOL_DELEGATION") in graph.typed

    def test_an_unresolvable_peer_yields_no_edge_rather_than_a_guess(self, tmp_path):
        # Endpoints are never invented: a name nothing answers to must leave the
        # joiner with one fewer predecessor, not with an edge to a stand-in.
        r = _enabled(tmp_path, task={"goal": "brief"})
        with r.branch("w1") as a:
            a.output = {"part": 1}
        with r.join("merge", [a, "typo_worker"]) as m:
            m.output = {"document": "..."}
        graph = _graph(r)
        assert graph.predecessors("merge") == {"orchestrator", "w1"}

    def test_the_collect_span_is_stamped_after_the_arm_it_read(self, tmp_path):
        # The corpus's hand-written fan-in shipped a delegation that COMPLETED
        # 1.5s before the run it named started; reconstruction builds an edge
        # from the name alone and never notices. Edges this SDK emits have to
        # hold together in time, so nothing here rests on that tolerance.
        r = _fan_out_run(tmp_path)
        arms = _agent_spans(r)
        latest_arm = max(
            int(arms[f"write_{s}"]["endTimeUnixNano"]) for s in ("intro", "specs", "pricing")
        )
        collects = [t for t in _tool_spans(r) if t["name"].startswith("collect:")]
        assert len(collects) == 3
        assert all(int(t["startTimeUnixNano"]) >= latest_arm for t in collects)

    def test_the_delegation_adds_no_node_of_its_own(self, tmp_path):
        # A TOOL span carries an edge; if it ever became a run it would appear
        # as a phantom agent nobody wrote.
        graph = _graph(_fan_out_run(tmp_path))
        assert set(graph.runs) == {
            "orchestrator",
            "plan",
            "write_intro",
            "write_specs",
            "write_pricing",
            "merge",
            "publish",
        }


class TestRetryWasInvisible:
    """The failures `retry` exists for."""

    def test_attempts_written_as_steps_chain_but_never_close(self, tmp_path):
        """Steps are a chain, and a chain is still not a loop.

        Updated when name disambiguation landed: these attempts now carry
        distinct identities (write#1, qa#1, write#2, …) and therefore chain, where
        they used to collide on one gen_ai.agent.name and reconstruct as loose
        nodes. What `retry` adds is unchanged and is the whole point — the
        BACK-EDGE. Without it there is no SCC, so the loop machinery still has
        nothing to fire on and an iteration count cannot be inferred.
        """
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        for i in range(3):
            with r.step("write") as s:
                s.output = {"draft": i}
            with r.step("qa") as s:
                s.output = {"verdict": "reject"}
        graph = _graph(r)
        assert len(graph.runs) == 7
        assert graph.predecessors("write#2") == {"qa#1"}   # a chain, forward only
        assert not graph.has_cycle_through("write#1", "qa#1", "write#2")

    def test_a_same_agent_retry_written_as_steps_chains_instead_of_vanishing(self, tmp_path):
        """Three attempts at ONE agent used to produce three loose nodes.

        Spans sharing a gen_ai.agent.name get no edge between them, so the
        sequence the integrator wrote disappeared from the graph with no error —
        a silent failure. Disambiguation gives each attempt its own identity, so
        the chain the code actually expresses survives. Still no cycle: that is
        `retry`'s job, pinned in TestRetryLoopReconstructs.
        """
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        for i in range(3):
            with r.step("fix") as s:
                s.output = {"try": i}
        graph = _graph(r)
        assert len(graph.runs) == 4
        assert graph.edges == {("editor", "fix#1"), ("fix#1", "fix#2"), ("fix#2", "fix#3")}
        assert not graph.has_cycle_through("fix#1", "fix#2", "fix#3")


class TestRetryLoopReconstructs:
    def test_three_attempts_close_into_a_cycle(self, tmp_path):
        # THE fix: numbered attempts chain, and the last one's result flows back
        # into the controller that decided to loop — which is the cycle.
        graph = _graph(_retry_run(tmp_path))
        assert graph.has_cycle_through(
            "revise_loop", "write#1", "qa#1", "write#2", "qa#2", "write#3", "qa#3"
        )
        assert ("qa#3", "revise_loop", "TOOL_DELEGATION") in graph.typed
        assert graph.predecessors("write#2") == {"qa#1"}

    def test_a_single_agent_retry_is_visible_as_a_loop(self, tmp_path):
        # The shape that used to produce zero edges.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        with r.retry("fix_loop") as loop:
            for i in range(3):
                with loop.attempt("fix") as a:
                    a.output = {"try": i}
            loop.output = {"try": 2}
        graph = _graph(r)
        assert graph.has_cycle_through("fix_loop", "fix#1", "fix#2", "fix#3")

    def test_one_pass_is_not_a_loop(self, tmp_path):
        # A body that ran once did not iterate. Drawing a cycle around it would
        # invent structure that never executed — and hand the loop-anomaly
        # detector an iteration count out of thin air.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        with r.retry("fix_loop") as loop:
            with loop.attempt("fix") as a:
                a.output = {"try": 0}
            loop.output = {"try": 0}
        graph = _graph(r)
        assert graph.edges == {("editor", "fix_loop"), ("fix_loop", "fix#1")}

    def test_a_one_pass_body_of_several_agents_is_still_not_a_loop(self, tmp_path):
        # Two different agents once each is a chain, not an iteration: the test
        # is whether some agent RAN TWICE, not how many attempts were opened.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        with r.retry("revise_loop") as loop:
            with loop.attempt("write") as a:
                a.output = {"draft": 0}
            with loop.attempt("qa") as a:
                a.output = {"verdict": "accept"}
            loop.output = {"draft": 0}
        graph = _graph(r)
        assert graph.predecessors("revise_loop") == {"editor"}

    def test_each_attempt_reads_the_previous_one(self, tmp_path):
        # Without the handoff every attempt looks like it started from nothing
        # and blame cannot tell a retry that improved from one that did not.
        graph = _graph(_retry_run(tmp_path))
        assert json.loads(graph.runs["write#2"].input) == {"verdict": "reject"}
        assert json.loads(graph.runs["qa#1"].input) == {"draft": 0}

    def test_an_attempt_after_a_silent_one_records_no_input(self, tmp_path):
        # Falling back to the loop's input would claim a handoff that did not
        # happen — and a contract check reading it would find the original
        # parameters intact on work that never received them.
        r = _enabled(tmp_path, "editor", task={"goal": "note", "format": "markdown"})
        with r.retry("revise_loop") as loop:
            with loop.attempt("fix"):
                pass  # crashed, produced nothing anyone recorded
            with loop.attempt("fix") as second:
                second.output = {"try": 1}
            loop.output = {"try": 1}
        spans = _agent_spans(r)
        assert json.loads(spans["fix#1"]["attrs"]["input.value"]) == {
            "goal": "note",
            "format": "markdown",
        }
        assert spans["fix#2"]["attrs"]["input.value"] == ""

    def test_an_attempt_records_which_agent_it_belongs_to(self, tmp_path):
        # The numbered name is the node identity, so the fact that write#1 and
        # write#2 are one agent survives only as an attribute.
        spans = _agent_spans(_retry_run(tmp_path))
        assert spans["write#2"]["attrs"]["agent_detective.attempt"] == "2"
        assert spans["write#2"]["attrs"]["agent_detective.attempt_of"] == "write"

    def test_the_loop_controller_output_is_not_invented(self, tmp_path):
        # The loop's result is whatever the controller returned, which the SDK
        # does not know. Unset stays unset — recorded as unscored, never as the
        # last attempt's output.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        with r.retry("revise_loop") as loop:
            for i in range(2):
                with loop.attempt("fix") as a:
                    a.output = {"try": i}
        assert _agent_spans(r)["revise_loop"]["attrs"]["output.value"] == ""

    def test_the_back_edge_is_stamped_after_the_attempt_it_reads(self, tmp_path):
        # The corpus's loop cells shipped a back-edge whose delegation finished
        # 1.5s BEFORE its target started; reconstruction built the cycle anyway,
        # from the name. This loop is a loop in time as well as in shape.
        r = _retry_run(tmp_path)
        last = _agent_spans(r)["qa#3"]
        back = [t for t in _tool_spans(r) if t["name"].startswith("loop_result:")]
        assert [t["attrs"]["gen_ai.tool.target_agent"] for t in back] == ["qa#3"]
        assert int(back[0]["startTimeUnixNano"]) >= int(last["endTimeUnixNano"])

    def test_an_attempt_left_open_is_closed_before_the_loop_ends(self, tmp_path):
        # An event-driven integration can miss a "finished" callback. The
        # back-edge still must not predate its target.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        loop = r.retry("revise_loop")
        loop.attempt("fix")
        loop.attempt("fix")
        loop.close(output={"try": 1})
        spans = _agent_spans(r)
        back = [t for t in _tool_spans(r) if t["name"].startswith("loop_result:")][0]
        assert int(back["startTimeUnixNano"]) >= int(spans["fix#2"]["endTimeUnixNano"])

    def test_a_loop_inside_a_branch_hangs_off_that_branch(self, tmp_path):
        # A retry inside one arm of a fan-out belongs to that arm. Chaining it
        # to the last pipeline step instead would attach the loop to a node that
        # never dispatched it.
        r = _enabled(tmp_path, task={"goal": "brief"})
        with r.step("plan") as p:
            p.output = {"sections": 2}
        with r.branch("write_specs") as arm:
            arm.output = {"section": "specs"}
            with r.retry("polish_loop") as loop:
                for i in range(2):
                    with loop.attempt("polish") as a:
                        a.output = {"pass": i}
                loop.output = {"pass": 1}
        graph = _graph(r)
        # The arm dispatched it; the last attempt reported back into it.
        assert graph.predecessors("polish_loop") == {"write_specs", "polish#2"}

    def test_the_loop_is_one_stage_of_the_enclosing_pipeline(self, tmp_path):
        # What follows the loop continues from the controller, never from an
        # attempt: a step after the retry did not read attempt #3's draft, it
        # read what the loop returned.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        with r.retry("revise_loop") as loop:
            for i in range(2):
                with loop.attempt("fix") as a:
                    a.output = {"try": i}
            loop.output = {"try": 1}
        with r.step("publish") as pub:
            pub.output = {"url": "u"}
        graph = _graph(r)
        assert graph.predecessors("publish") == {"revise_loop"}
        assert json.loads(graph.runs["publish"].input) == {"try": 1}


class TestRetryEventDriven:
    """Frameworks fire start and finish as separate callbacks."""

    def test_attempts_open_and_close_from_callbacks(self, tmp_path):
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        loop = r.retry("revise_loop")
        for i in range(2):
            loop.attempt("write")
            loop.end("write", output={"draft": i})     # plain name, not write#2
            loop.attempt("qa")
            loop.end("qa", output={"verdict": "reject"})
        loop.close(output={"draft": 1})
        graph = _graph(r)
        assert graph.has_cycle_through("revise_loop", "write#1", "qa#1", "write#2", "qa#2")

    def test_a_stray_finish_callback_is_ignored(self, tmp_path):
        # It must not close some other attempt, nor invent one.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        loop = r.retry("revise_loop")
        loop.attempt("write")
        assert loop.end("qa", output="x") is None
        loop.close()
        assert "qa#1" not in _agent_spans(r)


class TestSafety:
    def test_a_source_that_shares_the_readers_name_records_no_edge(self, tmp_path):
        # Resolution is by NAME: it would address either this run (dropped) or a
        # same-named sibling chosen by start time — a confident edge to the
        # wrong node.
        r = _enabled(tmp_path, task={"goal": "brief"})
        with r.branch("merge") as first:
            first.output = {"part": 1}
        with r.join("merge", [first]) as m:
            m.output = {"document": "..."}
        assert _tool_spans(r) == []

    def test_disabled_runs_emit_nothing(self, tmp_path, monkeypatch):
        # Off unless switched on: the constructs must stay as free as the rest.
        monkeypatch.delenv("AGENT_DETECTIVE_ENDPOINT", raising=False)
        monkeypatch.delenv("AGENT_DETECTIVE_TRACE_FILE", raising=False)
        r = run("orchestrator", task={"goal": "brief"})
        with r.branch("w1") as w:
            w.output = {"part": 1}
        with r.join("merge", [w]) as m:
            m.output = {"document": "..."}
        with r.retry("fix_loop") as loop:
            for i in range(2):
                with loop.attempt("fix") as a:
                    a.output = {"try": i}
        r.close()
        assert r.enabled is False
        assert list(tmp_path.iterdir()) == []

    def test_an_exception_inside_the_loop_marks_the_controller_and_propagates(
        self, tmp_path
    ):
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        with pytest.raises(ValueError):
            with r.retry("revise_loop") as loop:
                with loop.attempt("fix") as a:
                    a.output = {"try": 0}
                raise ValueError("budget exhausted")
        span = _agent_spans(r)["revise_loop"]
        assert span["status"]["code"] == 2
        assert "budget exhausted" in span["attrs"]["output.value"]

    def test_a_join_from_one_of_two_same_named_arms_edges_to_that_arm(self, tmp_path):
        """The edge must land on the arm that was passed, not the earlier one.

        Reconstruction resolves a delegation target by NAME, so two arms called
        `w1` used to collapse onto whichever started first: the graph said arm #1
        fed the merge while the payload carried arm #2's text. Blame walking back
        from a bad merge then reached the innocent arm. Asserting only that a
        warning was logged (what this test did before) left the wrong edge
        unpinned — the warning was true and the edge was still wrong.
        """
        r = _enabled(tmp_path, task={"goal": "brief"})
        with r.branch("w1") as first:
            first.output = {"part": 1}
        with r.branch("w1") as second:
            second.output = {"part": 2}
        with r.join("merge", [second]) as m:
            m.output = {"document": "..."}

        graph = _graph(r)
        assert ("w1#2", "merge", "TOOL_DELEGATION") in graph.typed
        assert ("w1#1", "merge", "TOOL_DELEGATION") not in graph.typed

    def test_a_map_reduce_join_keeps_every_arm(self, tmp_path):
        """Same-named arms are the canonical parallel map, and they were lossy.

        `_join_input` keyed a flat dict by agent name, so three arms called
        `worker` left ONE entry holding the last arm's output — presented as the
        complete merge input. A judge scored the merge against a third of what it
        merged, and two thirds of the evidence was deleted before blame ran.
        """
        r = _enabled(tmp_path, task={"goal": "summarise"})
        arms = []
        for i in range(3):
            with r.branch("worker") as w:
                w.output = {"doc": i}
            arms.append(w)
        with r.join("merge", arms) as m:
            m.output = {"document": "..."}

        merged = json.loads(_agent_spans(r)["merge"]["attrs"]["input.value"])
        assert merged == {"worker": [{"doc": 0}, {"doc": 1}, {"doc": 2}]}
        graph = _graph(r)
        for arm in ("worker#1", "worker#2", "worker#3"):
            assert (arm, "merge", "TOOL_DELEGATION") in graph.typed

    def test_a_branch_of_an_unfinished_dispatcher_claims_no_handoff(self, tmp_path):
        """An arm's input must not be borrowed from the dispatcher's own input.

        When the fan-out point has produced nothing yet, what the arm received is
        unknown. Substituting the dispatcher's input claims a handoff that never
        happened — and a contract check reading it finds the original parameters
        intact on work that never received them. `Retry.attempt` already refused
        this; `branch` inherited the opposite habit from `span`.
        """
        r = _enabled(tmp_path, task={"goal": "x", "format": "markdown"})
        outer = r.branch("research")          # still open, no output yet
        with r.branch("lookup", of=outer) as inner:
            inner.output = "found"
        outer.end()

        assert _agent_spans(r)["lookup"]["attrs"]["input.value"] == ""


@needs_engine
class TestAgainstTheShippedClassifier:
    """The mapper's edges, run through the topology contract the product uses."""

    def test_the_retry_reports_a_strongly_connected_component(self, tmp_path):
        graph = _graph(_retry_run(tmp_path))
        shape = classify_topology(sorted(graph.runs), sorted(graph.edges))
        assert shape["scc_count"] == 1
        assert shape["primary"] == "pipeline_with_feedback"

    def test_the_step_written_retry_reports_none(self, tmp_path):
        # Same code path, same classifier: the difference is the construct.
        r = _enabled(tmp_path, "editor", task={"goal": "note"})
        for i in range(3):
            with r.step("write") as s:
                s.output = {"draft": i}
            with r.step("qa") as s:
                s.output = {"verdict": "reject"}
        graph = _graph(r)
        shape = classify_topology(sorted(graph.runs), sorted(graph.edges))
        assert shape["scc_count"] == 0
        assert shape["primary"] == "pipeline"

    def test_the_fan_in_is_a_dag_with_a_real_merge_point(self, tmp_path):
        graph = _graph(_fan_out_run(tmp_path))
        shape = classify_topology(sorted(graph.runs), sorted(graph.edges))
        assert shape["scc_count"] == 0
        assert shape["primary"] == "dag"
        # Not a star: the arms converge on a node that reads all of them.
        assert len(graph.predecessors("merge")) == 4


class TestParallelRetryArms:
    """Two loops running side by side — the shape topology 18 (two independent
    loops joined at the end) is built from, and the one `retry` could not draw.

    A loop opened while another arm is still running took ``_open[-1]`` as its
    parent, so whichever thread reached ``retry()`` first became the other's
    predecessor: an edge no execution performed, in a graph whose whole job is
    to say which node fed which. ``parallel=True`` is the same declaration
    ``branch`` already makes, for a loop instead of a single step.
    """

    def _two_loops(self, tmp_path):
        """research loop || draft loop -> merge, arms interleaved as threads
        would interleave them."""
        r = _enabled(tmp_path, "conference_talk", task={"goal": "talk on EDA"})
        research = r.retry("research_loop", parallel=True, input={"topic": "EDA"})
        draft = r.retry("draft_loop", parallel=True, input={"topic": "EDA"})
        for i in range(2):
            with research.attempt("research_planner") as a:
                a.output = {"next": f"subtopic {i}"}
            with draft.attempt("draft_writer") as a:
                a.output = {"text": f"draft {i}"}
        research.close({"sources": 4})
        draft.close({"text": "draft 1"})
        with r.join("deck_merger", [research.span, draft.span]) as m:
            m.output = {"deck": "slides + notes"}
        return r

    def test_neither_arm_parents_on_the_other(self, tmp_path) -> None:
        g = _graph(self._two_loops(tmp_path))
        assert "draft_loop" not in g.predecessors("research_loop")
        assert "research_loop" not in g.predecessors("draft_loop")
        # Both hang off the run root, which is what actually dispatched them.
        # (Their own last attempt is the other predecessor — the loop back-edge.)
        assert "conference_talk" in g.predecessors("research_loop")
        assert "conference_talk" in g.predecessors("draft_loop")

    def test_each_arm_keeps_its_own_loop(self, tmp_path) -> None:
        """Parallel must not cost the arms their cycles."""
        g = _graph(self._two_loops(tmp_path))
        assert g.has_cycle_through("research_loop", "research_planner#1",
                                   "research_planner#2")
        assert g.has_cycle_through("draft_loop", "draft_writer#1", "draft_writer#2")

    def test_the_join_reads_both_arms(self, tmp_path) -> None:
        g = _graph(self._two_loops(tmp_path))
        assert {"research_loop", "draft_loop"} <= g.predecessors("deck_merger")

    def test_a_parallel_loop_is_not_the_chain_head(self, tmp_path) -> None:
        """A following `step` continues from the fan-out point, not from
        whichever arm happened to run last — the rule `branch` already keeps."""
        r = _enabled(tmp_path, "run")
        with r.step("plan") as p:
            p.output = {"plan": 1}
        arm = r.retry("polish_loop", parallel=True)
        with arm.attempt("polisher") as a:
            a.output = {"clean": True}
        arm.close({"clean": True})
        with r.step("publish") as pub:
            pub.output = {"url": "x"}
        g = _graph(r)
        assert g.predecessors("publish") == {"plan"}

    def test_sequential_retry_still_chains(self, tmp_path) -> None:
        """The default is unchanged: a plain loop IS a stage of the pipeline."""
        r = _enabled(tmp_path, "run")
        with r.step("plan") as p:
            p.output = {"plan": 1}
        with r.retry("revise") as loop:
            with loop.attempt("write") as a:
                a.output = {"draft": 1}
            loop.output = {"draft": 1}
        with r.step("publish") as pub:
            pub.output = {"url": "x"}
        g = _graph(r)
        assert g.predecessors("revise") == {"plan"}
        assert g.predecessors("publish") == {"revise"}

    def test_of_names_the_dispatching_step(self, tmp_path) -> None:
        r = _enabled(tmp_path, "run")
        with r.step("plan") as p:
            p.output = {"plan": 1}
        with r.step("collect") as c:
            c.output = {"docs": 3}
        loop = r.retry("revise", of=p)
        with loop.attempt("write") as a:
            a.output = {"draft": 1}
        loop.close({"draft": 1})
        g = _graph(r)
        assert g.predecessors("revise") == {"plan"}
