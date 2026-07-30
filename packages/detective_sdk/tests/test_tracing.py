"""Tests for the span-emission layer — the shortcut for code with no
instrumentation yet.

The point of these tests is the CONVENTIONS, not the plumbing: every one of
them pins something that, if silently wrong, produces a confident but wrong
analysis rather than an obvious error.

* No ``AGENT`` kind -> the span never becomes a node at all.
* No handoff on the input -> blame has nothing to compare between neighbours.
* ``step`` chains (pipeline), ``span`` nests (tree) — topology decides how the
  analysis reads the graph.
* Omitted cost stays absent, never 0 — an unmetered run must not look free.
* Off by default: without the env vars, instrumented code ships untouched.
* Never raises: an observability bug must not take the run down.
"""

from __future__ import annotations

import json

import pytest

from detective_sdk import run
from detective_sdk.tracing import MAX_PAYLOAD_CHARS


def _spans(r) -> dict[str, dict]:
    payload = r.build_payload()
    out = {}
    for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        attrs = {a["key"]: a["value"]["stringValue"] for a in span["attributes"]}
        out[attrs["gen_ai.agent.name"]] = {**span, "attrs": attrs}
    return out


def _enabled(tmp_path, name="run", **kw):
    return run(name, trace_file=str(tmp_path / "trace.json"), **kw)


class TestConventions:
    def test_every_span_is_an_agent_span(self, tmp_path):
        # Without AGENT the node does not exist: auto-instrumentation emits
        # CHAIN and Agent Detective ignores it.
        r = _enabled(tmp_path, task="brief")
        with r.step("resolve") as s:
            s.output = "acme"
        spans = _spans(r)
        assert all(v["attrs"]["openinference.span.kind"] == "AGENT" for v in spans.values())

    def test_root_carries_the_original_request(self, tmp_path):
        # The terminal judge cites this as provenance; without it there is
        # nothing to check the deliverable against.
        r = _enabled(tmp_path, "intel", task="pre-call dossier for Alza.cz")
        with r.step("resolve"):
            pass
        assert "Alza.cz" in _spans(r)["intel"]["attrs"]["input.value"]

    def test_root_has_no_output_of_its_own(self, tmp_path):
        # A wrapper legitimately produces nothing; Agent Detective records that
        # as unscored, NOT as a failure.
        r = _enabled(tmp_path, "orchestrator", task="brief")
        with r.step("work"):
            pass
        assert _spans(r)["orchestrator"]["attrs"]["output.value"] == ""

    def test_output_is_what_the_step_produced(self, tmp_path):
        r = _enabled(tmp_path)
        with r.step("collect") as s:
            s.output = {"docs": ["a", "b"], "financials": []}
        data = json.loads(_spans(r)["collect"]["attrs"]["output.value"])
        assert data["docs"] == ["a", "b"] and data["financials"] == []


class TestTopology:
    def test_step_chains_to_the_previous_step(self, tmp_path):
        # PIPELINE: resolve -> collect -> enrich.
        r = _enabled(tmp_path)
        for name in ("resolve", "collect", "enrich"):
            with r.step(name) as s:
                s.output = name
        spans = _spans(r)
        assert spans["collect"]["parentSpanId"] == spans["resolve"]["spanId"]
        assert spans["enrich"]["parentSpanId"] == spans["collect"]["spanId"]

    def test_step_input_defaults_to_the_previous_output(self, tmp_path):
        # Without the handoff each node looks like it started from nothing and
        # blame has no neighbour to compare against.
        r = _enabled(tmp_path)
        with r.step("resolve") as s:
            s.output = {"ico": "27082440"}
        with r.step("collect") as s:
            s.output = "docs"
        assert "27082440" in _spans(r)["collect"]["attrs"]["input.value"]

    def test_explicit_input_wins_over_the_handoff(self, tmp_path):
        r = _enabled(tmp_path)
        with r.step("resolve") as s:
            s.output = "ignored"
        with r.step("collect", input="explicit brief") as s:
            s.output = "docs"
        assert _spans(r)["collect"]["attrs"]["input.value"] == "explicit brief"

    def test_span_nests_under_the_enclosing_span(self, tmp_path):
        # TREE: planner wraps writer.
        r = _enabled(tmp_path)
        with r.span("planner") as p:
            p.output = "plan"
            with r.span("writer") as w:
                w.output = "draft"
        spans = _spans(r)
        assert spans["writer"]["parentSpanId"] == spans["planner"]["spanId"]

    def test_sibling_spans_share_the_root(self, tmp_path):
        r = _enabled(tmp_path, "orchestrator")
        with r.span("a"):
            pass
        with r.span("b"):
            pass
        spans = _spans(r)
        root = spans["orchestrator"]["spanId"]
        assert spans["a"]["parentSpanId"] == root and spans["b"]["parentSpanId"] == root

    def test_out_of_order_close_does_not_unbalance_nesting(self, tmp_path):
        # Concurrency: an outer span can finish after an inner one.
        r = _enabled(tmp_path)
        outer = r.span("outer")
        inner = r.span("inner")
        outer.__exit__(None, None, None)
        with r.span("after_outer"):
            pass
        inner.__exit__(None, None, None)
        spans = _spans(r)
        assert spans["after_outer"]["parentSpanId"] == spans["inner"]["spanId"]


class TestEventDriven:
    """Existing systems hook callbacks; there is no block to wrap."""

    def test_start_and_finish_arrive_as_separate_callbacks(self, tmp_path):
        r = _enabled(tmp_path, "intel", task="brief")
        # on_phase_start / on_phase_finish, as a framework would fire them.
        r.step("resolve")
        r.end("resolve", output={"ico": "27082440"})
        r.step("collect")
        r.end("collect", output={"docs": 14})
        spans = _spans(r)
        assert "27082440" in spans["resolve"]["attrs"]["output.value"]
        # The handoff still chains, exactly as with `with`.
        assert spans["collect"]["parentSpanId"] == spans["resolve"]["spanId"]
        assert "27082440" in spans["collect"]["attrs"]["input.value"]

    def test_finish_without_start_is_ignored(self, tmp_path):
        # A stray callback must not invent a zero-length span.
        r = _enabled(tmp_path)
        assert r.end("never_opened", output="x") is None
        assert "never_opened" not in _spans(r)

    def test_finish_can_mark_degraded(self, tmp_path):
        r = _enabled(tmp_path)
        r.step("people")
        r.end("people", output=[], failed=True)
        assert _spans(r)["people"]["status"]["code"] == 2

    def test_end_is_idempotent(self, tmp_path):
        # A framework that fires "finished" twice must not corrupt the record.
        r = _enabled(tmp_path)
        span = r.step("collect")
        span.end("first")
        first_end = span._end_ns
        span.end("second")
        assert span._end_ns == first_end
        assert span.output == "first"


class TestCost:
    def test_reported_cost_lands_on_the_span(self, tmp_path):
        r = _enabled(tmp_path)
        with r.step("synthesize") as s:
            s.output = "text"
            s.cost(usd=0.004, tokens_in=1200, tokens_out=340, model="gpt-4o")
        attrs = _spans(r)["synthesize"]["attrs"]
        assert attrs["gen_ai.usage.cost"] == "0.004"
        assert attrs["gen_ai.usage.input_tokens"] == "1200"
        assert attrs["gen_ai.request.model"] == "gpt-4o"

    def test_unreported_cost_is_absent_not_zero(self, tmp_path):
        # A metered agent must not look more expensive than an unmetered one.
        r = _enabled(tmp_path)
        with r.step("collect") as s:
            s.output = "docs"
        assert "gen_ai.usage.cost" not in _spans(r)["collect"]["attrs"]

    def test_partial_cost_reports_only_what_was_measured(self, tmp_path):
        r = _enabled(tmp_path)
        with r.step("collect") as s:
            s.cost(tokens_in=500)
        attrs = _spans(r)["collect"]["attrs"]
        assert attrs["gen_ai.usage.input_tokens"] == "500"
        assert "gen_ai.usage.cost" not in attrs
        assert "gen_ai.usage.output_tokens" not in attrs


class TestFailureAndArtifacts:
    def test_exception_marks_the_span_failed_and_propagates(self, tmp_path):
        r = _enabled(tmp_path)
        with pytest.raises(ValueError):
            with r.step("collect"):
                raise ValueError("upstream 500")
        span = _spans(r)["collect"]
        assert span["status"]["code"] == 2
        assert "upstream 500" in span["attrs"]["output.value"]

    def test_degraded_step_can_be_marked_without_raising(self, tmp_path):
        # A never-raise fallback still failed — tier1 needs to see it.
        r = _enabled(tmp_path)
        with r.step("people") as s:
            s.output = []
            s.fail()
        assert _spans(r)["people"]["status"]["code"] == 2

    def test_artifact_meta_rides_outside_the_payload(self, tmp_path):
        # Integrity read from the span attribute, never from the document text
        # (which its own content could forge).
        doc = tmp_path / "dossier.md"
        doc.write_text("# Alza", encoding="utf-8")
        r = _enabled(tmp_path)
        with r.step("render") as s:
            s.output = "# Alza"
            s.artifact(str(doc))
        meta = json.loads(_spans(r)["render"]["attrs"]["agent_detective.artifact_meta"])
        assert meta[0]["path"] == str(doc)
        assert meta[0]["size"] == len("# Alza".encode("utf-8"))

    def test_several_artifacts_accumulate(self, tmp_path):
        a, b = tmp_path / "a.md", tmp_path / "b.json"
        a.write_text("a", encoding="utf-8")
        b.write_text("{}", encoding="utf-8")
        r = _enabled(tmp_path)
        with r.step("render") as s:
            s.artifact(str(a)).artifact(str(b))
        meta = json.loads(_spans(r)["render"]["attrs"]["agent_detective.artifact_meta"])
        assert {m["path"] for m in meta} == {str(a), str(b)}

    def test_missing_artifact_does_not_raise(self, tmp_path):
        r = _enabled(tmp_path)
        with r.step("render") as s:
            s.artifact(str(tmp_path / "nope.md"))
        meta = json.loads(_spans(r)["render"]["attrs"]["agent_detective.artifact_meta"])
        assert meta[0]["detected_kind"] == "missing"

    def test_contract_params_ride_outside_the_payload(self, tmp_path):
        # The declared contract is the input side of the deterministic
        # contract check even when the step's payloads are prose/code.
        r = _enabled(tmp_path)
        with r.step("render") as s:
            s.output = "prose deliverable"
            s.contract(file_type="pdf", lang="cs")
        params = json.loads(
            _spans(r)["render"]["attrs"]["agent_detective.contract_params"]
        )
        assert params == {"file_type": "pdf", "lang": "cs"}

    def test_contract_params_accumulate_across_calls(self, tmp_path):
        r = _enabled(tmp_path)
        with r.step("render") as s:
            s.contract(file_type="pdf").contract(lang="cs", file_type="docx")
        params = json.loads(
            _spans(r)["render"]["attrs"]["agent_detective.contract_params"]
        )
        assert params == {"file_type": "docx", "lang": "cs"}


class TestOffByDefault:
    def test_no_env_no_export(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AGENT_DETECTIVE_ENDPOINT", raising=False)
        monkeypatch.delenv("AGENT_DETECTIVE_TRACE_FILE", raising=False)
        r = run("intel", task="brief")
        assert r.enabled is False
        with r.step("resolve") as s:
            s.output = "x"          # instrumented code still runs unchanged
        r.close()
        assert list(tmp_path.iterdir()) == []

    def test_env_switches_it_on(self, monkeypatch, tmp_path):
        target = tmp_path / "from_env.json"
        monkeypatch.setenv("AGENT_DETECTIVE_TRACE_FILE", str(target))
        with run("intel", task="brief") as r:
            with r.step("resolve") as s:
                s.output = "x"
        assert r.enabled is True
        assert json.loads(target.read_text(encoding="utf-8"))["resourceSpans"]


class TestNeverRaise:
    def test_unserializable_payload_still_produces_a_span(self, tmp_path):
        class Weird:
            def __repr__(self):
                return "<weird>"

        r = _enabled(tmp_path)
        with r.step("collect") as s:
            s.output = Weird()
        assert "weird" in _spans(r)["collect"]["attrs"]["output.value"]

    def test_unreachable_endpoint_does_not_raise(self, tmp_path):
        # Port 1 is reserved and never listening.
        with run("intel", task="x", endpoint="http://127.0.0.1:1") as r:
            with r.step("resolve") as s:
                s.output = "x"
        assert r.enabled is True  # tried, failed, stayed quiet

    def test_close_is_idempotent(self, tmp_path):
        target = tmp_path / "t.json"
        r = run("intel", task="x", trace_file=str(target))
        with r.step("a") as s:
            s.output = "1"
        r.close()
        first = target.read_text(encoding="utf-8")
        r.close()
        assert target.read_text(encoding="utf-8") == first


class TestTruncation:
    def test_oversized_payload_announces_the_cut(self, tmp_path):
        # A silently cut payload reads as "the agent produced this much".
        r = _enabled(tmp_path)
        with r.step("render") as s:
            s.output = "x" * (MAX_PAYLOAD_CHARS + 5_000)
        value = _spans(r)["render"]["attrs"]["output.value"]
        assert "truncated 5000 chars" in value


class TestExternalParent:
    def test_external_parent_span_id_rides_the_root(self, tmp_path):
        # A root parented on another process's span is what lets the re-map
        # build the structural SPAWN edge across processes.
        r = _enabled(tmp_path, "worker", task="x", parent_span_id="ABCDEF0123456789")
        with r.step("s") as s:
            s.output = "1"
        root = _spans(r)["worker"]
        assert root["parentSpanId"] == "abcdef0123456789"
        assert root["traceId"] == r.trace_id
        assert root["spanId"] == r.root_span_id

    def test_invalid_parent_span_id_means_no_parent(self, tmp_path):
        # Export is best-effort: a nonsense id must not take the run down, and
        # a root must not claim a parent that never existed.
        for bad in (None, "", "xyz", "0123456789abcdefg", "0123456789abcdez"):
            r = _enabled(tmp_path, parent_span_id=bad)
            with r.step("s") as s:
                s.output = "1"
            assert _spans(r)["run"]["parentSpanId"] == ""
