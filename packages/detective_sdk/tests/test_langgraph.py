"""Tests for the LangGraph adapter's automatic usage capture (FEAT-7C).

These pin the cost contract, not the plumbing: tokens aggregate per node, an
unknown price stays ABSENT (never 0), and a node that called no model carries
no usage attributes at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core", reason="langchain-core is not installed")

from detective_sdk.langgraph import DetectiveLangGraphHandler


class _Msg:
    def __init__(self, usage_metadata=None, response_metadata=None):
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


class _Gen:
    def __init__(self, message):
        self.message = message


class _LLMResult:
    def __init__(self, message=None, llm_output=None):
        self.generations = [[_Gen(message)]] if message else []
        self.llm_output = llm_output


def _result(tin, tout, model):
    return _LLMResult(
        message=_Msg(
            usage_metadata={"input_tokens": tin, "output_tokens": tout},
            response_metadata={"model_name": model},
        )
    )


def _spans(handler) -> dict[str, dict]:
    payload = handler.run.build_payload()
    out = {}
    for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        attrs = {a["key"]: a["value"]["stringValue"] for a in span["attributes"]}
        out[span["name"]] = attrs
    return out


def _run_one_node(handler, node="work", llm_results=()):
    """Simulate graph root -> one node -> the given LLM calls inside it."""
    handler.on_chain_start(None, {"q": 1}, run_id="g", parent_run_id=None, metadata={})
    handler.on_chain_start(None, {}, run_id="n", parent_run_id="g", metadata={"langgraph_node": node})
    for i, result in enumerate(llm_results):
        handler.on_llm_end(result, run_id=f"l{i}", parent_run_id="n")
    handler.on_chain_end({}, run_id="n")
    return _spans(handler)[node]


@pytest.fixture
def make_handler(tmp_path):
    def _make(**kwargs):
        return DetectiveLangGraphHandler(trace_file=str(tmp_path / "trace.json"), **kwargs)

    return _make


class TestUsageCapture:
    def test_node_with_llm_call_records_tokens_and_model(self, make_handler):
        attrs = _run_one_node(make_handler(), llm_results=[_result(10, 4, "gpt-x")])
        assert attrs["gen_ai.usage.input_tokens"] == "10"
        assert attrs["gen_ai.usage.output_tokens"] == "4"
        assert attrs["gen_ai.request.model"] == "gpt-x"

    def test_node_with_two_llm_calls_sums_tokens(self, make_handler):
        attrs = _run_one_node(
            make_handler(),
            llm_results=[_result(10, 4, "gpt-x"), _result(7, 3, "m2")],
        )
        assert attrs["gen_ai.usage.input_tokens"] == "17"
        assert attrs["gen_ai.usage.output_tokens"] == "7"
        # Two different models: no single name is the truth, so none is written.
        assert "gen_ai.request.model" not in attrs

    def test_usage_without_pricing_leaves_cost_unknown(self, make_handler):
        attrs = _run_one_node(make_handler(), llm_results=[_result(10, 4, "gpt-x")])
        # Unknown is absent, never 0: 0 claims the run was metered and free.
        assert "gen_ai.usage.cost" not in attrs

    def test_pricing_covering_all_calls_computes_cost(self, make_handler):
        handler = make_handler(pricing={"gpt-x": (2.0, 8.0)})  # USD per 1M tokens
        attrs = _run_one_node(handler, llm_results=[_result(1_000_000, 500_000, "gpt-x")])
        assert float(attrs["gen_ai.usage.cost"]) == pytest.approx(6.0)

    def test_pricing_missing_a_call_leaves_cost_unknown(self, make_handler):
        handler = make_handler(pricing={"gpt-x": (2.0, 8.0)})
        attrs = _run_one_node(
            handler,
            llm_results=[_result(10, 4, "gpt-x"), _result(7, 3, "unpriced")],
        )
        # A partial sum reads as the node's whole spend — worse than none.
        assert "gen_ai.usage.cost" not in attrs
        assert attrs["gen_ai.usage.input_tokens"] == "17"

    def test_node_without_llm_call_has_no_usage_attributes(self, make_handler):
        attrs = _run_one_node(make_handler())
        assert not any(k.startswith("gen_ai.usage") for k in attrs)

    def test_llm_call_nested_below_node_attributes_to_node(self, make_handler):
        handler = make_handler()
        handler.on_chain_start(None, {"q": 1}, run_id="g", parent_run_id=None, metadata={})
        handler.on_chain_start(None, {}, run_id="n", parent_run_id="g", metadata={"langgraph_node": "work"})
        handler.on_chain_start(None, {}, run_id="inner", parent_run_id="n", metadata={})
        handler.on_llm_end(_result(5, 2, "gpt-x"), run_id="l0", parent_run_id="inner")
        handler.on_chain_end({}, run_id="n")
        assert _spans(handler)["work"]["gen_ai.usage.input_tokens"] == "5"
