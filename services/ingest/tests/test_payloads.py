"""Payload routing: inline vs object-store overflow (PAYLOAD_INLINE_MAX_KB)."""

import asyncio
import copy

from ingest.types import graph_id_from_str, run_id_from_key
from conftest import Harness, load_fixture

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ORCHESTRATOR_KEY = f"{TRACE_ID}:00000000000000a1"


def _big_output_payload(size: int) -> dict:
    """spawn_pipeline with the orchestrator output inflated past 64 KB."""
    payload = copy.deepcopy(load_fixture("spawn_pipeline.json"))
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["spanId"] == "00000000000000a1"
    for attr in span["attributes"]:
        if attr["key"] == "output.value":
            attr["value"]["stringValue"] = "x" * size
            return payload
    raise AssertionError("output.value attribute not found in fixture")


def test_large_output_overflows_to_object_store(harness: Harness) -> None:
    big_output = "x" * 200_000
    payload = _big_output_payload(200_000)

    response = asyncio.run(harness.post_traces(payload))

    assert response.status_code == 200
    graph_id = graph_id_from_str("g-spawn-1")
    run_id = run_id_from_key(ORCHESTRATOR_KEY)
    run = harness.repo.runs[run_id]

    # Full payload in the object store under the spec'd key layout.
    expected_key = f"payloads/{graph_id}/{run_id}/output"
    assert ("agent-detective-payloads", expected_key) in harness.store.objects
    assert harness.store.objects[("agent-detective-payloads", expected_key)] == big_output.encode(
        "utf-8"
    )
    assert run.output_overflow_ref == f"s3://agent-detective-payloads/{expected_key}"
    assert run.output_bytes == 200_000
    # Inline keeps a bounded prefix; the summary is a short UI preview.
    assert run.output_inline is not None
    assert len(run.output_inline.encode("utf-8")) <= 64 * 1024
    assert big_output.startswith(run.output_inline)
    assert run.output_summary == big_output[:500]

    # The small input stays fully inline with no overflow.
    assert run.input_overflow_ref is None
    assert run.input_inline == "Find three products and translate them."


def test_payload_at_the_inline_limit_stays_inline(harness: Harness) -> None:
    limit = harness.settings.payload_inline_max_bytes
    payload = _big_output_payload(limit)

    response = asyncio.run(harness.post_traces(payload))

    assert response.status_code == 200
    run = harness.repo.runs[run_id_from_key(ORCHESTRATOR_KEY)]
    assert run.output_overflow_ref is None
    assert run.output_inline == "x" * limit
    assert run.output_bytes == limit
    assert harness.store.objects == {}


def test_small_payloads_never_touch_object_storage(harness: Harness) -> None:
    response = asyncio.run(harness.post_traces(load_fixture("spawn_pipeline.json")))

    assert response.status_code == 200
    assert harness.store.objects == {}
    for run in harness.repo.runs.values():
        assert run.input_overflow_ref is None
        assert run.output_overflow_ref is None
