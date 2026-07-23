"""Tests for the opt-in control hook against a local http.server stub.

The stub plays the API service's GET /control/breakers. The contract under
test: True only on a positively confirmed open breaker scoped to the agent's
name; every failure mode (network, HTTP, JSON, shape) returns False, because
observability must never take the agent down.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from detective_sdk import TOOL_SCHEMA_HASH_ATTRIBUTE, should_halt


@pytest.fixture
def breaker_server():
    """A local stub serving GET /control/breakers; yields (endpoint, state).

    ``state`` is a mutable dict: set ``state['body']`` to raw response bytes
    and ``state['status']`` to the HTTP status for the next request.
    """
    state = {"body": b'{"breakers":[]}', "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            assert self.path == "/control/breakers"
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):  # silence test output
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _set_breakers(state: dict, rows: list[dict]) -> None:
    state["body"] = json.dumps({"breakers": rows}).encode("utf-8")


def test_open_breaker_for_agent_halts(breaker_server) -> None:
    endpoint, state = breaker_server
    _set_breakers(
        state,
        [
            {
                "scope_kind": "agent_name",
                "scope_value": "translator",
                "state": "open",
                "reason": "3 open incidents",
            }
        ],
    )
    assert should_halt(endpoint, "translator") is True


def test_closed_breaker_does_not_halt(breaker_server) -> None:
    endpoint, state = breaker_server
    _set_breakers(
        state,
        [{"scope_kind": "agent_name", "scope_value": "translator", "state": "closed"}],
    )
    assert should_halt(endpoint, "translator") is False


def test_open_breaker_for_other_agent_does_not_halt(breaker_server) -> None:
    endpoint, state = breaker_server
    _set_breakers(
        state,
        [{"scope_kind": "agent_name", "scope_value": "scraper", "state": "open"}],
    )
    assert should_halt(endpoint, "translator") is False


def test_open_breaker_with_other_scope_kind_does_not_halt(breaker_server) -> None:
    # agent_version-scoped breakers are not matched by the name-scoped hook.
    endpoint, state = breaker_server
    _set_breakers(
        state,
        [{"scope_kind": "agent_version", "scope_value": "translator", "state": "open"}],
    )
    assert should_halt(endpoint, "translator") is False


def test_matching_row_among_many_halts(breaker_server) -> None:
    endpoint, state = breaker_server
    _set_breakers(
        state,
        [
            "junk",
            {"scope_kind": "agent_name", "scope_value": "scraper", "state": "closed"},
            {"scope_kind": "agent_name", "scope_value": "translator", "state": "open"},
        ],
    )
    assert should_halt(endpoint, "translator") is True


def test_trailing_slash_endpoint_accepted(breaker_server) -> None:
    endpoint, state = breaker_server
    _set_breakers(
        state,
        [{"scope_kind": "agent_name", "scope_value": "translator", "state": "open"}],
    )
    assert should_halt(endpoint + "/", "translator") is True


def test_empty_breakers_list(breaker_server) -> None:
    endpoint, _ = breaker_server
    assert should_halt(endpoint, "translator") is False


def test_invalid_json_returns_false(breaker_server) -> None:
    endpoint, state = breaker_server
    state["body"] = b"this is not json"
    assert should_halt(endpoint, "translator") is False


def test_unexpected_shape_returns_false(breaker_server) -> None:
    endpoint, state = breaker_server
    state["body"] = b'{"breakers": "nope"}'
    assert should_halt(endpoint, "translator") is False
    state["body"] = b"[1,2,3]"
    assert should_halt(endpoint, "translator") is False


def test_http_error_returns_false(breaker_server) -> None:
    endpoint, state = breaker_server
    state["status"] = 500
    state["body"] = b"boom"
    assert should_halt(endpoint, "translator") is False


def test_connection_error_returns_false() -> None:
    # Nothing listens on this port; the hook must swallow the failure.
    assert should_halt("http://127.0.0.1:9", "translator", timeout_s=0.5) is False


def test_attribute_constant() -> None:
    assert TOOL_SCHEMA_HASH_ATTRIBUTE == "agent_detective.tool_schema_hash"
