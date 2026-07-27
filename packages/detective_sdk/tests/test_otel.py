"""Tests for the OpenTelemetry bridge — the three failures that fail QUIETLY.

An existing OTEL system does not error out when its conventions are wrong; it
produces an empty or misshapen graph. Each test here pins one of those:

* CHAIN spans are not nodes at all until promoted.
* Promoted siblings have no edges between them until chained.
* Child LLM spans must keep their original parent, or tokens/cost roll up into
  the wrong node.

No `opentelemetry` install is needed: `normalize` reads duck-typed spans, so a
fake shaped like a ReadableSpan exercises the real code path.
"""

from __future__ import annotations

import json

from detective_sdk.otel import (
    AGENT_KIND_ATTRIBUTE,
    AGENT_NAME_ATTRIBUTE,
    SpanRecord,
    TraceCollector,
    chain_agents,
    normalize,
    promote_agents,
    to_export_request,
)


# --- a ReadableSpan lookalike ------------------------------------------------ #

class _Ctx:
    def __init__(self, trace_id: int, span_id: int):
        self.trace_id, self.span_id = trace_id, span_id


class _Code:
    def __init__(self, name: str):
        self.name = name


class _Status:
    def __init__(self, name: str):
        self.status_code = _Code(name)


class _Named:
    def __init__(self, name: str):
        self.name = name


class _Resource:
    def __init__(self, attributes: dict):
        self.attributes = attributes


class FakeSpan:
    """Shaped like `opentelemetry.sdk.trace.ReadableSpan`."""

    def __init__(self, name, span_id, *, parent_id=None, attributes=None, start=1, end=2,
                 status="OK", trace_id=0xABC, resource=None, scope="crewai"):
        self.name = name
        self._ctx = _Ctx(trace_id, span_id)
        self.parent = _Ctx(trace_id, parent_id) if parent_id is not None else None
        self.attributes = attributes or {}
        self.start_time, self.end_time = start, end
        self.status = _Status(status)
        self.kind = _Named("INTERNAL")
        self.resource = _Resource(resource or {"service.name": "crew"})
        self.instrumentation_scope = _Named(scope)

    def get_span_context(self):
        return self._ctx


def _crew_run() -> list[SpanRecord]:
    """What vanilla auto-instrumentation emits: CHAIN nodes + child LLM spans."""
    spans = [
        FakeSpan("kickoff", 0x10, start=1, end=99),
        FakeSpan("research_task", 0x20, parent_id=0x10, start=10, end=20,
                 attributes={"openinference.span.kind": "CHAIN"}),
        FakeSpan("llm-1", 0x21, parent_id=0x20, start=11, end=19,
                 attributes={"openinference.span.kind": "LLM",
                             "gen_ai.usage.input_tokens": 900}),
        FakeSpan("write_task", 0x30, parent_id=0x10, start=30, end=40,
                 attributes={"openinference.span.kind": "CHAIN"}),
        FakeSpan("llm-2", 0x31, parent_id=0x30, start=31, end=39,
                 attributes={"openinference.span.kind": "LLM"}),
    ]
    return [normalize(s) for s in spans]


def _by_name(records) -> dict[str, SpanRecord]:
    return {r.name: r for r in records}


class TestNormalize:
    def test_ids_become_hex_of_the_wire_width(self):
        record = normalize(FakeSpan("task", 0x20, parent_id=0x10))
        assert record.span_id == f"{0x20:016x}"
        assert record.parent_id == f"{0x10:016x}"
        assert record.trace_id == f"{0xABC:032x}"

    def test_rootless_span_has_empty_parent(self):
        assert normalize(FakeSpan("root", 0x10)).parent_id == ""

    def test_error_status_is_carried(self):
        assert normalize(FakeSpan("t", 0x1, status="ERROR")).error is True
        assert normalize(FakeSpan("t", 0x1, status="OK")).error is False

    def test_a_broken_span_is_dropped_not_fatal(self):
        class Exploding:
            def get_span_context(self):
                raise RuntimeError("boom")

        assert normalize(Exploding()) is None


class TestPromotion:
    def test_chain_spans_are_not_nodes_until_promoted(self):
        records = _crew_run()
        # As they arrive: nothing is an agent, so the graph would be empty.
        assert not any(r.is_agent for r in records)
        promoted = promote_agents(records, ["research_task", "write_task"])
        assert _by_name(promoted)["research_task"].is_agent
        assert _by_name(promoted)["write_task"].is_agent

    def test_promotion_sets_the_agent_name(self):
        promoted = promote_agents(_crew_run(), lambda s: "researcher"
                                  if s.name == "research_task" else None)
        record = _by_name(promoted)["research_task"]
        assert record.attributes[AGENT_NAME_ATTRIBUTE] == "researcher"
        assert record.attributes[AGENT_KIND_ATTRIBUTE] == "AGENT"

    def test_non_selected_spans_are_untouched(self):
        promoted = promote_agents(_crew_run(), ["research_task"])
        assert not _by_name(promoted)["llm-1"].is_agent
        assert not _by_name(promoted)["write_task"].is_agent

    def test_existing_agent_spans_are_left_alone(self):
        # Composes with partially-correct instrumentation.
        records = [normalize(FakeSpan("already", 0x1, attributes={
            AGENT_KIND_ATTRIBUTE: "AGENT", AGENT_NAME_ATTRIBUTE: "keep_me"}))]
        promoted = promote_agents(records, lambda s: "renamed")
        assert promoted[0].attributes[AGENT_NAME_ATTRIBUTE] == "keep_me"

    def test_a_raising_predicate_does_not_break_the_export(self):
        def boom(span):
            raise ValueError("bad predicate")

        promoted = promote_agents(_crew_run(), boom)
        assert len(promoted) == 5 and not any(r.is_agent for r in promoted)


class TestChaining:
    def test_promoted_siblings_get_edges(self):
        # Before chaining both hang off `kickoff` — nodes, but no path between.
        promoted = promote_agents(_crew_run(), ["research_task", "write_task"])
        before = _by_name(promoted)
        assert before["research_task"].parent_id == before["write_task"].parent_id

        chained = _by_name(chain_agents(promoted))
        assert chained["write_task"].parent_id == chained["research_task"].span_id

    def test_execution_order_decides_the_chain_not_arrival_order(self):
        promoted = promote_agents(_crew_run(), ["research_task", "write_task"])
        shuffled = list(reversed(promoted))
        chained = _by_name(chain_agents(shuffled))
        assert chained["write_task"].parent_id == chained["research_task"].span_id

    def test_first_agent_keeps_its_original_parent(self):
        # Overwriting it would orphan the graph from the framework's own root.
        promoted = promote_agents(_crew_run(), ["research_task", "write_task"])
        chained = _by_name(chain_agents(promoted))
        assert chained["research_task"].parent_id == f"{0x10:016x}"

    def test_child_llm_spans_keep_their_parent(self):
        # This is what makes tokens/cost roll up into the right node.
        promoted = promote_agents(_crew_run(), ["research_task", "write_task"])
        chained = _by_name(chain_agents(promoted))
        assert chained["llm-2"].parent_id == chained["write_task"].span_id

    def test_single_agent_run_is_unchanged(self):
        promoted = promote_agents(_crew_run(), ["research_task"])
        assert chain_agents(promoted) == promoted


class TestExportRequest:
    def test_shape_is_an_export_trace_service_request(self):
        payload = to_export_request(_crew_run(), service="crew")
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 5
        assert spans[0]["traceId"] == f"{0xABC:032x}"
        resource = payload["resourceSpans"][0]["resource"]["attributes"]
        assert {"key": "service.name", "value": {"stringValue": "crew"}} in resource

    def test_error_status_uses_the_code_the_mapper_reads(self):
        records = [normalize(FakeSpan("t", 0x1, status="ERROR"))]
        span = to_export_request(records)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["status"]["code"] == 2

    def test_typed_attributes_survive(self):
        records = [normalize(FakeSpan("t", 0x1, attributes={
            "gen_ai.usage.input_tokens": 900, "flag": True, "ratio": 0.5}))]
        attrs = {a["key"]: a["value"]
                 for a in to_export_request(records)["resourceSpans"][0]
                 ["scopeSpans"][0]["spans"][0]["attributes"]}
        assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "900"}
        assert attrs["flag"] == {"boolValue": True}
        assert attrs["ratio"] == {"doubleValue": 0.5}


class TestCollector:
    def _collector(self, tmp_path, **kw):
        return TraceCollector(trace_file=str(tmp_path / "trace.json"), **kw)

    def _written(self, tmp_path) -> dict:
        return json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))

    def test_buffers_then_exports_once_at_the_end(self, tmp_path):
        collector = self._collector(tmp_path, promote=["research_task", "write_task"],
                                    chain=True)
        # Spans arrive in batches, as a processor delivers them.
        spans = [FakeSpan("research_task", 0x20, parent_id=0x10, start=10, end=20,
                          attributes={"openinference.span.kind": "CHAIN"}),
                 FakeSpan("write_task", 0x30, parent_id=0x10, start=30, end=40,
                          attributes={"openinference.span.kind": "CHAIN"})]
        collector.export([spans[0]])
        assert not (tmp_path / "trace.json").exists()   # nothing sent mid-run
        collector.export([spans[1]])
        collector.shutdown()

        emitted = self._written(tmp_path)["resourceSpans"][0]["scopeSpans"][0]["spans"]
        by_name = {s["name"]: s for s in emitted}
        assert by_name["write_task"]["parentSpanId"] == by_name["research_task"]["spanId"]

    def test_flush_is_idempotent(self, tmp_path):
        collector = self._collector(tmp_path, promote=["a"])
        collector.export([FakeSpan("a", 0x1)])
        collector.flush()
        first = (tmp_path / "trace.json").read_text(encoding="utf-8")
        collector.flush()
        assert (tmp_path / "trace.json").read_text(encoding="utf-8") == first

    def test_nothing_collected_sends_nothing(self, tmp_path):
        self._collector(tmp_path).flush()
        assert not (tmp_path / "trace.json").exists()

    def test_task_adds_the_root_that_carries_provenance(self, tmp_path):
        # Without it the terminal judge has nothing to check the deliverable
        # against and the verdict degrades to not_checkable.
        collector = self._collector(tmp_path, promote=["research_task", "write_task"],
                                    chain=True, root="crew", task="Build a snake game")
        collector.export([FakeSpan("research_task", 0x20, parent_id=0x10, start=10, end=20),
                          FakeSpan("write_task", 0x30, parent_id=0x10, start=30, end=40)])
        collector.flush()

        emitted = self._written(tmp_path)["resourceSpans"][0]["scopeSpans"][0]["spans"]
        by_name = {s["name"]: s for s in emitted}
        root = by_name["crew"]
        attrs = {a["key"]: a["value"]["stringValue"] for a in root["attributes"]}
        assert "snake game" in attrs["input.value"]
        assert attrs["openinference.span.kind"] == "AGENT"
        assert root["parentSpanId"] == ""
        # The first real node now hangs off the synthesised root.
        assert by_name["research_task"]["parentSpanId"] == root["spanId"]

    def test_without_task_no_root_is_invented(self, tmp_path):
        collector = self._collector(tmp_path, promote=["a"])
        collector.export([FakeSpan("a", 0x1)])
        collector.flush()
        names = {s["name"] for s in
                 self._written(tmp_path)["resourceSpans"][0]["scopeSpans"][0]["spans"]}
        assert names == {"a"}
