"""Pure mapping from OTLP/HTTP JSON spans to agent-run and edge candidates.

This package is the graph-reconstruction core of Agent Detective (build spec
section 6.1). It is pure: no I/O, no network, standard library only. Functions
accept parsed JSON (plain dicts) and return immutable dataclasses.

Accepted input
--------------
``map_spans`` takes a flat list of span dicts. Two shapes are accepted and may
be mixed freely:

1. The OTLP/HTTP JSON span shape (as found inside an
   ExportTraceServiceRequest), with attributes in the OTLP key/value array
   form::

       {"traceId": "...", "spanId": "...", "parentSpanId": "...",
        "name": "...", "kind": 1,
        "startTimeUnixNano": "1752000000000000000",
        "endTimeUnixNano": "1752000009000000000",
        "attributes": [{"key": "gen_ai.agent.name",
                        "value": {"stringValue": "orchestrator"}}],
        "status": {"code": "STATUS_CODE_OK"}}

2. A flattened shape with snake_case keys and plain-dict attributes::

       {"trace_id": "...", "span_id": "...", "parent_span_id": "...",
        "name": "...", "kind": "CLIENT",
        "start_time": "2025-07-08T18:40:00Z", "end_time": "...",
        "attributes": {"gen_ai.agent.name": "orchestrator"},
        "status": "ok"}

Attributes are accepted either as the OTLP key/value array form
(``[{"key": k, "value": {"stringValue": v}}, ...]``) or as a plain dict of
already-decoded values; plain-dict values that still look like OTLP wrappers
are unwrapped defensively. Timestamps may be ISO-8601 strings or
unix-nanosecond strings (bare numbers are treated as unix nanoseconds).

``flatten_export_request`` converts a full OTLP ExportTraceServiceRequest
payload into the flat span list ``map_spans`` accepts, attaching each span's
resource attributes under the ``resource_attributes`` key. Agent name/version
lookup consults span attributes first, then resource attributes.

Run model
---------
Every span with attribute ``openinference.span.kind = AGENT`` opens exactly one
run. Every other span belongs to the run of its nearest AGENT-ancestor span
within the same trace; spans without an AGENT ancestor are not part of any run.

``run_key`` is derived deterministically as ``"<trace_id>:<span_id>"`` of the
AGENT span that opened the run. Ingest (M3) is expected to hash this key into
the ``agent_runs.run_id`` UUID (e.g. uuid5); the key is stable across
redelivery of the same spans, which makes retries idempotent.

Field extraction per run:

- ``agent_name`` / ``agent_version``: ``gen_ai.agent.name`` /
  ``gen_ai.agent.version``, span attributes first, then resource attributes.
  Missing values stay ``None`` — the mapper never invents identity.
- ``model_name``: ``gen_ai.request.model`` — opening AGENT span attributes
  first, then resource attributes, then the first member span in execution
  order carrying the attribute (standard GenAI semconv emits it on child LLM
  spans, not the AGENT span). ``None`` when absent everywhere.
- ``prompt_hash``: ``agent_detective.prompt_hash`` — opening AGENT span
  attributes, then resource attributes. ``None`` when absent.
- ``tool_schema_hash``: ``agent_detective.tool_schema_hash`` — opening AGENT
  span attributes, then resource attributes (same rule as ``prompt_hash``).
  ``None`` when absent.
- ``artifact_meta``: the raw ``agent_detective.artifact_meta`` string from
  the opening AGENT span attributes ONLY — no resource fallback, because it
  is per-run data: a resource-level value would smear one node's artifact
  onto every run exported under that resource. Never invented; ``None``
  when absent. The string is passed through verbatim (downstream parses it
  tolerantly).
- ``tokens_in`` / ``tokens_out``: ``gen_ai.usage.input_tokens`` /
  ``gen_ai.usage.output_tokens`` (OpenInference ``llm.token_count.prompt`` /
  ``llm.token_count.completion`` accepted as fallback). If the opening AGENT
  span carries the value it wins (avoids double counting); otherwise values
  are summed over all member spans. Absent everywhere -> ``None``.
- ``cost_usd``: ``gen_ai.usage.cost`` with the same AGENT-wins / children-sum
  rule. ``None`` when absent; this package deliberately ships no pricing
  table.
- ``tool_calls``: compact JSON digest of the run's member spans of kind TOOL
  (``openinference.span.kind == 'TOOL'``), in execution order (start time,
  span_id tiebreak). One entry per TOOL span:
  ``{"name": <gen_ai.tool.name attr, else span name>, "args_sha": <first 12
  hex chars of sha256 over input.value ('' when absent)>, "status": 'error'
  when the span status is ERROR else 'ok'}``. ``None`` when the run has no
  TOOL member spans — never an empty array, so absence stays distinguishable.
- ``input`` / ``output``: OpenInference ``input.value`` / ``output.value`` of
  the opening AGENT span. Non-string values are JSON-serialized.
- ``status``: ``"failed"`` when any member span reports an OTLP ERROR status,
  else ``"ok"`` (the schema constrains status to ok/degraded/failed, and
  "degraded" is a quality judgement made downstream, not by the mapper).
- ``start_time`` / ``end_time``: from the opening AGENT span (min/max over
  member spans as fallback), timezone-aware UTC datetimes.
- ``graph_id``: the correlation header (see below) when present on any member
  span, else the trace id (single-trace assumption).

Edge rules (build spec section 6.1)
-----------------------------------
Edges point in the direction of influence: ``from_run``'s output feeds
``to_run``. This is the direction ``blame_engine`` expects (a node's
predecessors explain its quality).

- ``SPAWN``: an AGENT-kind span whose parent span belongs to a run of a
  *different* agent. Edge parent-run -> child-run. When both agent names are
  known and equal, no edge is emitted (same-agent nested/retry AGENT spans).
  When either name is unknown the structural edge is still emitted — the
  mapper keeps structure it can see rather than silently dropping it, and the
  ``detection_method`` text records that names were unknown.
- ``TOOL_DELEGATION``: a TOOL-kind span carrying ``gen_ai.tool.target_agent``.
  The edge points from the *target* agent's run to the run owning the tool
  span, because the target's output flows back into the caller. The target
  run is resolved by agent name among the runs seen in the same mapping call
  (same trace preferred, then earliest start time, then run key). If no run
  matches the target name, no edge is emitted — endpoints are never invented.
- ``A2A_MESSAGE`` (only with ``a2a_detection=True``, default off): a span
  carrying ``a2a.task_id``, or an HTTP client span whose path ends with
  ``/.well-known/agent.json`` (A2A agent-card discovery). The peer run is
  resolved via the ``a2a.peer_agent`` attribute. The edge points from the peer
  (callee) to the caller, mirroring TOOL_DELEGATION, because the peer's
  response flows back to the caller; when the span is explicitly a SERVER
  span the direction is flipped. Without a resolvable peer, no edge.

Every edge records which rule fired in ``detection_method`` (free text).
Edges are deduplicated on ``(from_run_key, to_run_key, type)``; the first
rule to fire wins and processing follows input order, so the output is
deterministic for a given input.

Graph membership and the correlation-header limitation
------------------------------------------------------
``x-execution-graph-id`` (plain attribute or the OTEL HTTP header attribute
form ``http.request.header.x-execution-graph-id``) determines graph
*membership* only: all runs carrying the same value are grouped into one
execution graph, possibly across many traces. It says nothing about edge
*direction*, and this mapper deliberately derives no edges from it — a shared
header proves two agents participated in the same execution, not who called
whom. Callers must treat header-correlated graphs without structural edges as
a forest of independent runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .types import AgentRunCandidate, EdgeCandidate, EdgeType, MappingResult

__all__ = ["map_spans", "flatten_export_request"]

_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)

_OTLP_WRAPPER_KEYS = {
    "stringValue",
    "boolValue",
    "intValue",
    "doubleValue",
    "bytesValue",
    "arrayValue",
    "kvlistValue",
}

_SPAN_KIND_NAMES = {
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
    "INTERNAL": 1,
    "SERVER": 2,
    "CLIENT": 3,
    "PRODUCER": 4,
    "CONSUMER": 5,
}
_SPAN_KIND_SERVER = 2
_SPAN_KIND_CLIENT = 3

_GRAPH_HEADER_KEYS = (
    "x-execution-graph-id",
    "http.request.header.x-execution-graph-id",
)

_AGENT_CARD_SUFFIX = "/.well-known/agent.json"


@dataclass
class _Span:
    """Internal normalized span record."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int | None
    attrs: dict[str, Any]
    resource_attrs: dict[str, Any]
    start: datetime | None
    end: datetime | None
    error: bool
    index: int


@dataclass
class _RunAcc:
    """Mutable accumulator for one run while mapping."""

    key: str
    trace_id: str
    opener: _Span
    members: list[_Span]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def map_spans(
    spans: Iterable[dict[str, Any]] | None, *, a2a_detection: bool = False
) -> MappingResult:
    """Map a flat list of OTLP span dicts to run/edge candidates.

    See the module docstring for the accepted input shapes, the run model,
    and the edge detection rules. ``a2a_detection`` enables the A2A_MESSAGE
    rules (default off, per build spec section 6.1).
    """
    if not spans:
        return MappingResult()

    norm: list[_Span] = []
    for index, raw in enumerate(spans):
        span = _normalize_span(raw, index)
        if span is not None:
            norm.append(span)

    by_id: dict[tuple[str, str], _Span] = {}
    for s in norm:
        by_id.setdefault((s.trace_id, s.span_id), s)

    # Every AGENT-kind span opens exactly one run.
    accs: dict[str, _RunAcc] = {}
    opener_run: dict[tuple[str, str], str] = {}
    for s in norm:
        if _kind_label(s) != "AGENT":
            continue
        key = f"{s.trace_id}:{s.span_id}"
        opener_run[(s.trace_id, s.span_id)] = key
        if key not in accs:
            accs[key] = _RunAcc(key=key, trace_id=s.trace_id, opener=s, members=[s])

    def owner_run_key(span: _Span) -> str | None:
        """Run owning ``span``: nearest AGENT-ancestor within the same trace."""
        cur: _Span | None = span
        seen: set[tuple[str, str]] = set()
        while cur is not None:
            key = opener_run.get((cur.trace_id, cur.span_id))
            if key is not None:
                return key
            parent_id = cur.parent_span_id
            if not parent_id:
                return None
            ident = (cur.trace_id, parent_id)
            if ident in seen:  # cycle in broken parenting data
                return None
            seen.add(ident)
            cur = by_id.get(ident)
        return None

    # Assign non-AGENT spans to the run of their nearest AGENT ancestor.
    for s in norm:
        if (s.trace_id, s.span_id) in opener_run:
            continue
        key = owner_run_key(s)
        if key is not None:
            accs[key].members.append(s)

    candidates = {key: _build_run(acc) for key, acc in accs.items()}
    edges = _detect_edges(norm, candidates, by_id, opener_run, owner_run_key, a2a_detection)

    runs_sorted = sorted(candidates.values(), key=lambda c: (c.start_time or _MIN_TIME, c.run_key))
    edges_sorted = sorted(edges.values(), key=lambda e: (e.from_run_key, e.to_run_key, e.type.value))
    graph_ids = {c.graph_id for c in runs_sorted}
    return MappingResult(runs=runs_sorted, edges=edges_sorted, graph_ids=graph_ids)


def flatten_export_request(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a full OTLP ExportTraceServiceRequest payload to a span list.

    Each returned span keeps its original OTLP JSON shape and gains a
    ``resource_attributes`` key (plain dict) with the attributes of the
    resource the span was exported under. Non-dict entries are skipped.
    """
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    resource_spans = payload.get("resourceSpans") or payload.get("resource_spans") or []
    for rs in resource_spans:
        if not isinstance(rs, dict):
            continue
        resource = rs.get("resource")
        resource_attrs = (
            _flatten_attributes(resource.get("attributes")) if isinstance(resource, dict) else {}
        )
        scope_spans = rs.get("scopeSpans") or rs.get("scope_spans") or []
        for ss in scope_spans:
            if not isinstance(ss, dict):
                continue
            for sp in ss.get("spans") or []:
                if not isinstance(sp, dict):
                    continue
                flat = dict(sp)
                flat["resource_attributes"] = dict(resource_attrs)
                out.append(flat)
    return out


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _unwrap_value(value: Any) -> Any:
    """Decode one OTLP ``AnyValue`` wrapper into a plain Python value.

    Values that do not look like OTLP wrappers (plain primitives, or dicts
    without any known wrapper key) are returned unchanged, so plain-dict
    attribute forms pass through untouched.
    """
    if not isinstance(value, dict):
        return value
    if not (_OTLP_WRAPPER_KEYS & value.keys()):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "bytesValue" in value:
        return value["bytesValue"]
    if "arrayValue" in value:
        values = value["arrayValue"]
        if isinstance(values, dict):
            values = values.get("values", [])
        if isinstance(values, list):
            return [_unwrap_value(v) for v in values]
        return None
    if "kvlistValue" in value:
        kvl = value["kvlistValue"]
        if isinstance(kvl, dict):
            return _flatten_attributes(kvl.get("values", []))
        return None
    return None


def _flatten_attributes(attrs: Any) -> dict[str, Any]:
    """Accept attributes as an OTLP key/value array or a plain dict."""
    if isinstance(attrs, dict):
        return {str(k): _unwrap_value(v) for k, v in attrs.items()}
    out: dict[str, Any] = {}
    if isinstance(attrs, list):
        for item in attrs:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not isinstance(key, str) or not key:
                continue
            out[key] = _unwrap_value(item.get("value"))
    return out


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO-8601 strings, unix-nano strings, or bare nanoseconds to UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_unix_nanos(int(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return _from_unix_nanos(int(s))
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _from_unix_nanos(nanos: int) -> datetime:
    seconds, ns = divmod(nanos, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=ns // 1000)


def _parse_error(status: Any) -> bool:
    """OTLP status -> error flag. Accepts dict codes (int or str) and strings."""
    if isinstance(status, str):
        return status.strip().upper() in {"ERROR", "STATUS_CODE_ERROR"}
    if isinstance(status, dict):
        code = status.get("code")
        if isinstance(code, bool):
            return False
        if isinstance(code, int):
            return code == 2
        if isinstance(code, str):
            return code.strip().upper() in {"ERROR", "STATUS_CODE_ERROR"}
    return False


def _parse_kind(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _SPAN_KIND_NAMES.get(value.strip().upper())
    return None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _normalize_span(raw: Any, index: int) -> _Span | None:
    """Normalize one span dict (either accepted shape); None when unusable."""
    if not isinstance(raw, dict):
        return None
    trace_id = _first(raw, "traceId", "trace_id")
    span_id = _first(raw, "spanId", "span_id")
    if not trace_id or not span_id:
        return None
    parent = _first(raw, "parentSpanId", "parent_span_id")
    parent_id = str(parent) if parent is not None else ""
    # An all-zero parent id is the OTLP encoding of "no parent".
    if not parent_id or set(parent_id) == {"0"}:
        parent_span_id: str | None = None
    else:
        parent_span_id = parent_id
    return _Span(
        trace_id=str(trace_id),
        span_id=str(span_id),
        parent_span_id=parent_span_id,
        name=str(raw.get("name") or ""),
        kind=_parse_kind(raw.get("kind")),
        attrs=_flatten_attributes(raw.get("attributes")),
        resource_attrs=_flatten_attributes(_first(raw, "resource_attributes", "resourceAttributes")),
        start=_parse_timestamp(_first(raw, "startTimeUnixNano", "start_time", "start")),
        end=_parse_timestamp(_first(raw, "endTimeUnixNano", "end_time", "end")),
        error=_parse_error(raw.get("status")),
        index=index,
    )


def _kind_label(span: _Span) -> str:
    return str(span.attrs.get("openinference.span.kind") or "").strip().upper()


# ---------------------------------------------------------------------------
# Run building
# ---------------------------------------------------------------------------


def _first_str(*values: Any) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _metric(acc: _RunAcc, keys: tuple[str, ...], integer: bool) -> int | float | None:
    """AGENT-span value wins; otherwise sum over member spans; else None."""
    for k in keys:
        v = _num(acc.opener.attrs.get(k))
        if v is not None:
            return int(v) if integer else v
    total = 0.0
    found = False
    for m in acc.members:
        if m is acc.opener:
            continue
        for k in keys:
            v = _num(m.attrs.get(k))
            if v is not None:
                total += v
                found = True
                break
    if not found:
        return None
    return int(total) if integer else total


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _header_graph_id(attrs: dict[str, Any]) -> str | None:
    for k in _GRAPH_HEADER_KEYS:
        v = attrs.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _tool_calls_digest(acc: _RunAcc) -> str | None:
    """Compact JSON digest of the run's TOOL member spans; None when none.

    Execution order (start time, span_id tiebreak) so the digest is
    deterministic regardless of input order. ``args_sha`` is the first 12 hex
    chars of sha256 over the span's ``input.value`` ('' when absent) — enough
    to compare tool arguments across runs without shipping the arguments.
    """
    tools = [m for m in acc.members if _kind_label(m) == "TOOL"]
    if not tools:
        return None
    tools.sort(key=lambda m: (m.start or _MIN_TIME, m.span_id))
    digest = []
    for m in tools:
        name = _first_str(m.attrs.get("gen_ai.tool.name")) or m.name
        args = _text(m.attrs.get("input.value")) or ""
        digest.append(
            {
                "name": name,
                "args_sha": hashlib.sha256(args.encode("utf-8")).hexdigest()[:12],
                "status": "error" if m.error else "ok",
            }
        )
    return json.dumps(digest, separators=(",", ":"))


def _build_run(acc: _RunAcc) -> AgentRunCandidate:
    opener = acc.opener
    name = _first_str(
        opener.attrs.get("gen_ai.agent.name"), opener.resource_attrs.get("gen_ai.agent.name")
    )
    version = _first_str(
        opener.attrs.get("gen_ai.agent.version"),
        opener.resource_attrs.get("gen_ai.agent.version"),
    )
    model_name = _first_str(
        opener.attrs.get("gen_ai.request.model"),
        opener.resource_attrs.get("gen_ai.request.model"),
    )
    if model_name is None:
        # Standard GenAI semconv emits gen_ai.request.model on child LLM
        # spans, not the AGENT span: fall back to the first member span in
        # execution order carrying it. Ties on start time break on span_id
        # so the result is deterministic regardless of input order.
        for m in sorted(acc.members, key=lambda m: (m.start or _MIN_TIME, m.span_id)):
            if m is opener:
                continue
            model_name = _first_str(m.attrs.get("gen_ai.request.model"))
            if model_name is not None:
                break
    # Opening AGENT span attributes ONLY — no resource fallback: this is
    # per-run data, and a resource-level value would smear one node's
    # artifact metadata onto every run under that resource. Never invented.
    artifact_meta = _first_str(opener.attrs.get("agent_detective.artifact_meta"))
    prompt_hash = _first_str(
        opener.attrs.get("agent_detective.prompt_hash"),
        opener.resource_attrs.get("agent_detective.prompt_hash"),
    )
    tool_schema_hash = _first_str(
        opener.attrs.get("agent_detective.tool_schema_hash"),
        opener.resource_attrs.get("agent_detective.tool_schema_hash"),
    )
    tokens_in = _metric(acc, ("gen_ai.usage.input_tokens", "llm.token_count.prompt"), integer=True)
    tokens_out = _metric(
        acc, ("gen_ai.usage.output_tokens", "llm.token_count.completion"), integer=True
    )
    cost = _metric(acc, ("gen_ai.usage.cost",), integer=False)
    start = opener.start or min((m.start for m in acc.members if m.start), default=None)
    end = opener.end or max((m.end for m in acc.members if m.end), default=None)
    status = "failed" if any(m.error for m in acc.members) else "ok"
    graph_id = None
    for m in acc.members:
        graph_id = _header_graph_id(m.attrs)
        if graph_id:
            break
    return AgentRunCandidate(
        run_key=acc.key,
        graph_id=graph_id or acc.trace_id,
        trace_id=acc.trace_id,
        agent_name=name,
        agent_version=version,
        model_name=model_name,
        prompt_hash=prompt_hash,
        tool_schema_hash=tool_schema_hash,
        artifact_meta=artifact_meta,
        tool_calls=_tool_calls_digest(acc),
        tokens_in=tokens_in if tokens_in is None else int(tokens_in),
        tokens_out=tokens_out if tokens_out is None else int(tokens_out),
        cost_usd=cost if cost is None else float(cost),
        input=_text(opener.attrs.get("input.value")),
        output=_text(opener.attrs.get("output.value")),
        start_time=start,
        end_time=end,
        status=status,
    )


# ---------------------------------------------------------------------------
# Edge detection
# ---------------------------------------------------------------------------


def _http_path(span: _Span) -> str | None:
    for key in ("url.full", "http.url"):
        url = span.attrs.get(key)
        if isinstance(url, str) and url:
            try:
                path = urlsplit(url).path
            except ValueError:
                path = ""
            if path:
                return path
    for key in ("url.path", "http.target"):
        path = span.attrs.get(key)
        if isinstance(path, str) and path:
            return path
    return None


def _is_http_client(span: _Span) -> bool:
    if span.kind == _SPAN_KIND_CLIENT:
        return True
    return any(
        k in span.attrs for k in ("http.request.method", "http.method", "url.full", "http.url")
    )


def _is_agent_card_fetch(span: _Span) -> bool:
    if not _is_http_client(span):
        return False
    path = _http_path(span)
    return bool(path) and path.rstrip("/").endswith(_AGENT_CARD_SUFFIX)


def _detect_edges(
    norm: list[_Span],
    candidates: dict[str, AgentRunCandidate],
    by_id: dict[tuple[str, str], _Span],
    opener_run: dict[tuple[str, str], str],
    owner_run_key: Callable[[_Span], str | None],
    a2a_detection: bool,
) -> dict[tuple[str, str, EdgeType], EdgeCandidate]:
    edges: dict[tuple[str, str, EdgeType], EdgeCandidate] = {}

    def emit(edge: EdgeCandidate) -> None:
        # Dedup on (from, to, type); first rule to fire wins. Processing
        # follows input order, so the outcome is deterministic.
        edges.setdefault((edge.from_run_key, edge.to_run_key, edge.type), edge)

    def resolve_by_name(name: str, trace_id: str) -> AgentRunCandidate | None:
        matches = [c for c in candidates.values() if c.agent_name == name]
        if not matches:
            return None
        matches.sort(key=lambda c: (c.trace_id != trace_id, c.start_time or _MIN_TIME, c.run_key))
        return matches[0]

    # Rule 1: SPAWN — AGENT span parented inside a different agent's run.
    for s in norm:
        child_key = opener_run.get((s.trace_id, s.span_id))
        if child_key is None or not s.parent_span_id:
            continue
        parent = by_id.get((s.trace_id, s.parent_span_id))
        if parent is None:
            continue
        parent_key = owner_run_key(parent)
        if parent_key is None or parent_key == child_key:
            continue
        parent_name = candidates[parent_key].agent_name
        child_name = candidates[child_key].agent_name
        if parent_name and child_name and parent_name == child_name:
            continue
        note = "" if parent_name and child_name else " (agent name unknown; structural parentage only)"
        emit(
            EdgeCandidate(
                from_run_key=parent_key,
                to_run_key=child_key,
                type=EdgeType.SPAWN,
                detection_method="rule=spawn: openinference.span.kind=AGENT with parent span "
                "owned by a different agent run" + note,
            )
        )

    # Rule 2: TOOL_DELEGATION — TOOL span naming a target agent.
    # Edge points target -> caller: the target's output flows back into the
    # run that owns the tool span.
    for s in norm:
        if _kind_label(s) != "TOOL":
            continue
        target = s.attrs.get("gen_ai.tool.target_agent")
        if not isinstance(target, str) or not target.strip():
            continue
        owner_key = owner_run_key(s)
        if owner_key is None:
            continue
        target_run = resolve_by_name(target.strip(), s.trace_id)
        if target_run is None or target_run.run_key == owner_key:
            continue
        emit(
            EdgeCandidate(
                from_run_key=target_run.run_key,
                to_run_key=owner_key,
                type=EdgeType.TOOL_DELEGATION,
                detection_method=f"rule=tool_delegation: gen_ai.tool.target_agent='{target.strip()}' "
                "on TOOL span; edge points target -> caller (direction of data flow)",
            )
        )

    # Rule 3: A2A_MESSAGE — feature-flagged (build spec: A2A_DETECTION=false).
    if a2a_detection:
        for s in norm:
            has_task_id = bool(s.attrs.get("a2a.task_id"))
            card_fetch = _is_agent_card_fetch(s)
            if not has_task_id and not card_fetch:
                continue
            peer = s.attrs.get("a2a.peer_agent")
            if not isinstance(peer, str) or not peer.strip():
                continue
            owner_key = owner_run_key(s)
            if owner_key is None:
                continue
            peer_run = resolve_by_name(peer.strip(), s.trace_id)
            if peer_run is None or peer_run.run_key == owner_key:
                continue
            # Client-side span: the peer's response flows back to the caller
            # (peer -> caller). Server-side span: the owner processes the task
            # and its output flows to the peer (owner -> peer).
            if s.kind == _SPAN_KIND_SERVER:
                from_key, to_key = owner_key, peer_run.run_key
            else:
                from_key, to_key = peer_run.run_key, owner_key
            if has_task_id:
                method = "rule=a2a_message: a2a.task_id attribute present"
            else:
                method = (
                    "rule=a2a_message: HTTP client span on /.well-known/agent.json "
                    "(A2A agent card fetch)"
                )
            emit(EdgeCandidate(from_key, to_key, EdgeType.A2A_MESSAGE, method))

    return edges
