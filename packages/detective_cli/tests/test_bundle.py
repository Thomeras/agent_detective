"""Trace loading and OTLP -> GraphBundle mapping."""

from __future__ import annotations

import json

import pytest

from detective_cli.bundle import (
    TraceFormatError,
    bundles_from_exports,
    load_trace,
)
from otel_mapper import graph_id_from_str, run_id_from_key

from conftest import export, linear_pipeline, span


class TestLoadTrace:
    def test_reads_a_single_export_object(self, trace_file):
        path = trace_file(linear_pipeline())
        assert len(load_trace(path)) == 1

    def test_reads_a_json_array_of_exports(self, trace_file):
        path = trace_file([linear_pipeline(), linear_pipeline()])
        assert len(load_trace(path)) == 2

    def test_reads_json_lines(self, tmp_path):
        # What a batching exporter appends: one export per line, no array.
        path = tmp_path / "trace.jsonl"
        path.write_text(
            "\n".join(json.dumps(linear_pipeline()) for _ in range(3)) + "\n",
            encoding="utf-8",
        )
        assert len(load_trace(path)) == 3

    def test_blank_lines_in_json_lines_are_skipped(self, tmp_path):
        path = tmp_path / "trace.jsonl"
        path.write_text(f"\n{json.dumps(linear_pipeline())}\n\n", encoding="utf-8")
        assert len(load_trace(path)) == 1

    def test_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("   \n", encoding="utf-8")
        with pytest.raises(TraceFormatError, match="empty"):
            load_trace(path)

    def test_malformed_json_names_the_offending_line(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"a": 1}\nnot json at all\n', encoding="utf-8")
        with pytest.raises(TraceFormatError, match="line 2"):
            load_trace(path)

    def test_a_bare_scalar_is_rejected(self, tmp_path):
        path = tmp_path / "scalar.json"
        path.write_text("42", encoding="utf-8")
        with pytest.raises(TraceFormatError, match="expected an OTLP export"):
            load_trace(path)

    def test_missing_file_is_reported_not_raised_as_oserror(self, tmp_path):
        with pytest.raises(TraceFormatError, match="cannot read"):
            load_trace(tmp_path / "nope.json")


class TestBundles:
    def test_builds_one_bundle_per_graph_with_chained_edges(self):
        bundles = bundles_from_exports([linear_pipeline()])
        assert len(bundles) == 1
        bundle = bundles[0]
        assert [r.agent_name for r in bundle.runs] == ["planner", "writer", "reviewer"]
        assert len(bundle.edges) == 2
        assert bundle.graph_type == "test-pipeline"
        assert bundle.run_count == 3

    def test_edges_point_along_the_parent_child_chain(self):
        bundle = bundles_from_exports([linear_pipeline()])[0]
        by_id = {r.run_id: r.agent_name for r in bundle.runs}
        pairs = {(by_id[e.from_run_id], by_id[e.to_run_id]) for e in bundle.edges}
        assert pairs == {("planner", "writer"), ("writer", "reviewer")}

    def test_run_ids_are_the_shared_uuid5_derivation(self):
        # Ingest and the CLI must agree on identity, or a report produced one
        # way cannot be compared with one produced the other way.
        bundle = bundles_from_exports([linear_pipeline()])[0]
        expected = run_id_from_key(f"{'1' * 32}:{1:016x}")
        assert any(r.run_id == expected for r in bundle.runs)

    def test_graph_id_is_derived_from_the_trace_id(self):
        bundle = bundles_from_exports([linear_pipeline()])[0]
        assert bundle.graph_id == graph_id_from_str("1" * 32)

    def test_payloads_stay_inline_with_no_overflow_ref(self):
        # There is no object store locally; a bundle that claimed an overflow
        # ref would send resolve_payload looking for a bucket that isn't there.
        bundle = bundles_from_exports([linear_pipeline()])[0]
        for run in bundle.runs:
            assert run.output_overflow_ref is None
            assert run.input_overflow_ref is None
            assert run.output_inline

    def test_output_bytes_measures_the_encoded_payload(self):
        bundle = bundles_from_exports(
            [linear_pipeline({"writer": "prodej za 100 Kč — příliš"})]
        )[0]
        writer = next(r for r in bundle.runs if r.agent_name == "writer")
        assert writer.output_bytes == len("prodej za 100 Kč — příliš".encode("utf-8"))

    def test_spans_split_across_exports_reconstruct_as_one_graph(self):
        # A batching exporter flushes mid-run; the graph must not split in two.
        full = linear_pipeline()
        spans = full["resourceSpans"][0]["scopeSpans"][0]["spans"]
        first = export(spans[:2])
        second = export(spans[2:])
        bundles = bundles_from_exports([first, second])
        assert len(bundles) == 1
        assert len(bundles[0].runs) == 3

    def test_two_traces_produce_two_bundles(self):
        other = linear_pipeline()
        for s in other["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            s["traceId"] = "2" * 32
        bundles = bundles_from_exports([linear_pipeline(), other])
        assert len(bundles) == 2

    def test_bundle_order_is_deterministic(self):
        other = linear_pipeline()
        for s in other["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            s["traceId"] = "2" * 32
        first = [b.graph_id for b in bundles_from_exports([linear_pipeline(), other])]
        second = [b.graph_id for b in bundles_from_exports([other, linear_pipeline()])]
        assert first == second

    def test_a_trace_without_agent_spans_yields_no_bundles(self):
        llm_only = span(name="chat", span_id="0" * 16, agent_name=None)
        llm_only["attributes"][0]["value"]["stringValue"] = "LLM"
        assert bundles_from_exports([export([llm_only])]) == []

    def test_total_cost_sums_the_runs_that_reported_one(self):
        payload = linear_pipeline()
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        for s, cost in zip(spans, ("0.01", "0.02", "0.03")):
            s["attributes"].append(
                {"key": "gen_ai.usage.cost", "value": {"stringValue": cost}}
            )
        bundle = bundles_from_exports([payload])[0]
        assert bundle.total_cost_usd == pytest.approx(0.06)

    def test_total_cost_is_none_when_no_run_reported_one(self):
        # Absent cost data must not render as $0.00 — that is a claim.
        assert bundles_from_exports([linear_pipeline()])[0].total_cost_usd is None

    def test_contract_params_attribute_reaches_the_run_record(self):
        # span.contract(...) lands as agent_detective.contract_params; local
        # mode must carry it into the RunRecord, or the deterministic contract
        # check silently never runs on a check the deployed ingest does store.
        declared = '{"price": "$12/user/month"}'
        spans = [
            span(
                name="writer.run",
                span_id="a" * 16,
                agent_name="writer",
                extra_attributes={"agent_detective.contract_params": declared},
            )
        ]
        bundle = bundles_from_exports([export(spans)])[0]
        run = next(r for r in bundle.runs if r.agent_name == "writer")
        assert run.contract_params == declared
