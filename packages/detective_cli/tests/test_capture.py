"""The OTLP receiver: what it accepts, what it refuses, and what it says.

Every test drives a real socket on an ephemeral port — the point of this
component is that a stock OTLP exporter can talk to it, so mocking the
transport would test the wrong thing. Nothing here reaches the network beyond
loopback.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from detective_cli.capture import Capture, serve

from conftest import linear_pipeline


def post(address: str, body: bytes, content_type: str = "application/json"):
    request = urllib.request.Request(
        f"{address}/v1/traces",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def post_expecting_error(address: str, body: bytes, content_type: str = "application/json"):
    try:
        post(address, body, content_type)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    raise AssertionError("expected an HTTP error")


class Receiver:
    """Runs `serve` on an ephemeral port in a thread; `--once` ends it."""

    def __init__(self, **kwargs):
        self.capture = Capture()
        self.address: str | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._kwargs = kwargs
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        serve(
            self.capture,
            host="127.0.0.1",
            port=0,  # ephemeral: tests must not fight over a fixed port
            on_ready=self._on_ready,
            stop=self._stop,
            **self._kwargs,
        )

    def _on_ready(self, address: str):
        self.address = address
        self._ready.set()

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(timeout=5), "server did not start"
        return self

    def __exit__(self, *exc):
        # Tests that never complete a capture (a rejected request, a health
        # probe) would otherwise sit until the thread's own timeout.
        self._stop.set()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "server did not shut down"


@pytest.fixture
def receiver():
    with Receiver(once=True) as r:
        yield r


class TestAccepting:
    def test_it_accepts_an_export_and_counts_its_spans(self, receiver):
        status, body = post(receiver.address, json.dumps(linear_pipeline()).encode())
        assert status == 200
        # The OTLP success shape: an exporter that does not see it logs an
        # error, which would look like a failure on the agent's side.
        assert body == {"partialSuccess": {}}
        receiver._thread.join(timeout=5)
        assert receiver.capture.spans == 3
        assert len(receiver.capture.exports) == 1

    def test_the_captured_export_analyses_into_a_graph(self, receiver):
        post(receiver.address, json.dumps(linear_pipeline()).encode())
        receiver._thread.join(timeout=5)
        from detective_cli.bundle import bundles_from_exports

        bundles = bundles_from_exports(receiver.capture.exports)
        assert len(bundles) == 1
        assert [r.agent_name for r in bundles[0].runs] == ["planner", "writer", "reviewer"]

    def test_health_answers_before_any_trace_arrives(self, receiver):
        with urllib.request.urlopen(f"{receiver.address}/health", timeout=5) as response:
            assert json.loads(response.read()) == {"status": "ok", "spans": 0}


class TestRefusing:
    def test_protobuf_is_named_as_the_problem_with_the_fix(self, receiver):
        # The single most likely first mistake: Python's stock exporter sends
        # protobuf. "400 bad request" would send the user hunting.
        status, body = post_expecting_error(
            receiver.address, b"\x0a\x0b\x08", "application/x-protobuf"
        )
        assert status == 415
        assert "OTEL_EXPORTER_OTLP_PROTOCOL=http/json" in body["error"]

    def test_protobuf_without_its_header_is_still_recognised(self, receiver):
        status, body = post_expecting_error(receiver.address, b"\x0a\x0b\x08\x01\x12")
        assert status == 415
        assert "protobuf" in body["error"]

    def test_malformed_json_is_reported_as_such(self, receiver):
        status, body = post_expecting_error(receiver.address, b'{"broken": ')
        assert status == 400
        assert "not valid JSON" in body["error"]

    def test_a_json_array_is_not_an_export_request(self, receiver):
        status, body = post_expecting_error(receiver.address, b"[1, 2, 3]")
        assert status == 400
        assert "ExportTraceServiceRequest" in body["error"]

    def test_an_empty_body_is_rejected(self, receiver):
        status, _ = post_expecting_error(receiver.address, b"")
        assert status == 400

    def test_the_wrong_path_says_which_path_is_right(self, receiver):
        request = urllib.request.Request(
            f"{receiver.address}/v1/logs", data=b"{}", method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert "/v1/traces" in json.loads(exc.read())["error"]
        else:
            raise AssertionError("expected 404")

    def test_a_rejected_request_does_not_end_a_once_capture(self, receiver):
        # A protobuf attempt must leave the server listening for the retry.
        post_expecting_error(receiver.address, b"\x0a\x0b", "application/x-protobuf")
        status, _ = post(receiver.address, json.dumps(linear_pipeline()).encode())
        assert status == 200


class TestAccumulation:
    def test_several_exports_accumulate_into_one_run(self):
        # A batching exporter flushes more than once; the spans belong to one
        # graph and must not be analysed as separate partial runs.
        capture = Capture()
        full = linear_pipeline()
        spans = full["resourceSpans"][0]["scopeSpans"][0]["spans"]
        from conftest import export

        capture.add(export(spans[:2]))
        capture.add(export(spans[2:]))
        assert capture.spans == 3

        from detective_cli.bundle import bundles_from_exports

        bundles = bundles_from_exports(capture.exports)
        assert len(bundles) == 1
        assert len(bundles[0].runs) == 3

    def test_an_export_with_no_spans_counts_zero_but_is_kept(self):
        capture = Capture()
        assert capture.add({"resourceSpans": []}) == 0
        assert capture.empty is False

    def test_a_fresh_capture_is_empty(self):
        assert Capture().empty is True
