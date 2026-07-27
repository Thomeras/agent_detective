"""Receive OTLP/HTTP JSON traces over a socket, with no stack behind it.

The analysis reads a trace; something has to produce one. In a deployment that
is the ingest service, which needs Postgres, ClickHouse, Redis and MinIO behind
it. Locally that is a heavy price for "let me look at this one run", and
writing a bespoke file exporter is a heavy price for the agent project.

So this is the smallest thing that closes the gap: a standard-library HTTP
server speaking the one endpoint an OTLP exporter calls, ``POST /v1/traces``.
Any already-instrumented agent points its existing exporter at it — no code
change, no new dependency, nothing to install on the agent's side — and the
spans land here instead of in a database.

Two deliberate limits, because this is a local tool and not a service:

- it binds to **loopback** by default (an endpoint that accepts trace payloads
  should not appear on every interface because a default said so), and
- it holds the run in memory and never persists anything unless asked, so
  there is no state to reason about between runs.

Protobuf is rejected with an actionable message rather than a 400. Python's
stock ``OTLPSpanExporter`` serializes protobuf, and pointing it here is the
single most likely first mistake — the error says exactly which environment
variable fixes it.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

# A trace is payload text plus attributes; a few MB is ordinary and a very
# chatty run can be far more. The cap exists only so a malformed
# Content-Length cannot make the process allocate without bound.
MAX_BODY_BYTES = 256 * 1024 * 1024

_PROTOBUF_HELP = (
    "this endpoint accepts OTLP/HTTP JSON, and the request looks like "
    "protobuf. Python's stock OTLPSpanExporter serializes protobuf; set "
    "OTEL_EXPORTER_OTLP_PROTOCOL=http/json (or use a JSON exporter) and "
    "point OTEL_EXPORTER_OTLP_ENDPOINT at this server."
)


@dataclass
class Capture:
    """The exports received so far.

    A batching exporter sends several requests per run, so exports accumulate
    and the analysis runs over all of them together — spans split across
    flushes still reconstruct as one graph.
    """

    exports: list[dict[str, Any]] = field(default_factory=list)
    spans: int = 0

    def add(self, payload: dict[str, Any]) -> int:
        """Record one export request; returns how many spans it carried."""
        count = 0
        for resource_span in payload.get("resourceSpans") or []:
            for scope_span in resource_span.get("scopeSpans") or []:
                count += len(scope_span.get("spans") or [])
        self.exports.append(payload)
        self.spans += count
        return count

    @property
    def empty(self) -> bool:
        return not self.exports


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Injected by the factory below.
    capture: Capture
    on_export: Callable[[int], None]
    verbose: bool

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # BaseHTTPRequestHandler logs every request to stderr; that noise would
        # bury the report this command exists to print.
        if self.verbose:
            super().log_message(fmt, *args)

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            self._send(200, {"status": "ok", "spans": self.capture.spans})
            return
        self._send(404, {"error": f"no such path: {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/traces":
            self._send(404, {"error": f"no such path: {self.path} (expected /v1/traces)"})
            return

        content_type = (self.headers.get("Content-Type") or "").lower()
        if "protobuf" in content_type:
            self._send(415, {"error": _PROTOBUF_HELP})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "malformed Content-Length"})
            return
        if length <= 0:
            self._send(400, {"error": "empty request body"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": f"body exceeds {MAX_BODY_BYTES} bytes"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            # A protobuf body that arrived without the header lands here; say
            # the useful thing rather than "invalid JSON".
            if raw[:1] not in (b"{", b"["):
                self._send(415, {"error": _PROTOBUF_HELP})
            else:
                self._send(400, {"error": "request body is not valid JSON"})
            return

        if not isinstance(payload, dict):
            self._send(400, {"error": "expected an ExportTraceServiceRequest object"})
            return

        received = self.capture.add(payload)
        # The OTLP success shape. Exporters check for it and log an error
        # otherwise, which would look like a failure on the agent's side.
        self._send(200, {"partialSuccess": {}})
        self.on_export(received)


def serve(
    capture: Capture,
    *,
    host: str = "127.0.0.1",
    port: int = 8900,
    once: bool = False,
    verbose: bool = False,
    on_export: Callable[[int], None] | None = None,
    on_ready: Callable[[str], None] | None = None,
    stop: threading.Event | None = None,
) -> Capture:
    """Run the receiver until interrupted (or until one export arrives).

    ``once`` matches the common collecting-exporter shape — buffer the run,
    POST it all on shutdown — so a scripted capture needs no timeout and no
    Ctrl-C. ``stop`` lets a caller that is not a terminal (a test, an embedding
    program) end the capture without a signal.
    """
    stop = stop if stop is not None else threading.Event()

    def _exported(count: int) -> None:
        if on_export is not None:
            on_export(count)
        if once:
            stop.set()

    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"capture": capture, "on_export": staticmethod(_exported), "verbose": verbose},
    )

    server = ThreadingHTTPServer((host, port), handler)
    # Do not keep the process alive for a request that never ends.
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if on_ready is not None:
        bound_host, bound_port = server.server_address[:2]
        on_ready(f"http://{bound_host}:{bound_port}")

    try:
        # Ctrl-C is the ordinary way to end an interactive capture; it is a
        # completed capture, not an error.
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return capture
