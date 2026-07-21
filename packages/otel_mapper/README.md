# otel_mapper

Pure mapping from OTLP/HTTP JSON spans to `AgentRun` / `Edge` candidates
(Agent Detective build spec section 6.1). No I/O, no network, standard
library only. License: Apache-2.0.

## Usage

```python
from otel_mapper import flatten_export_request, map_spans

spans = flatten_export_request(otlp_export_request_payload)  # full OTLP JSON
result = map_spans(spans)                    # a2a_detection=False by default
result.runs        # list[AgentRunCandidate], sorted by (start_time, run_key)
result.edges       # list[EdgeCandidate], sorted by (from, to, type)
result.graph_ids   # set[str]
```

`map_spans` also accepts a flat list of span dicts directly, in either the
OTLP/HTTP JSON shape (`traceId`/`spanId`/`attributes` key-value array) or a
flattened snake_case shape with plain-dict attributes. Timestamps may be
ISO-8601 strings or unix-nanosecond strings. See `otel_mapper/mapper.py`'s
module docstring for the full input contract.

## Keying (contract with ingest / M3)

- `run_key = "<trace_id>:<span_id>"` of the AGENT span that opened the run.
  Deterministic and stable across redelivery; ingest hashes it into
  `agent_runs.run_id` (e.g. uuid5).
- Every `openinference.span.kind = AGENT` span opens exactly one run; other
  spans join the run of their nearest AGENT ancestor in the same trace.
- `graph_id` is the `x-execution-graph-id` correlation header when present on
  any member span (plain attribute or `http.request.header.x-execution-graph-id`),
  else the trace id.

## Edge rules and direction

Edges point in the direction of influence (`from_run` output feeds `to_run`),
which is what `blame_engine` expects.

| Type | Rule | Direction |
|---|---|---|
| `SPAWN` | AGENT span whose parent belongs to a different agent's run | parent run -> child run |
| `TOOL_DELEGATION` | TOOL span with `gen_ai.tool.target_agent` | target agent's run -> caller run |
| `A2A_MESSAGE` | `a2a.task_id` attribute, or HTTP client span on `/.well-known/agent.json`; only with `a2a_detection=True` | peer run -> caller run (flipped for SERVER spans) |

Edges carry a free-text `detection_method` recording which rule fired, and are
deduplicated on `(from_run_key, to_run_key, type)`.

## Correlation-header limitation

`x-execution-graph-id` determines graph **membership only**. It groups runs
into one execution graph, possibly across many traces, but says nothing about
who called whom. The mapper deliberately derives **no edges** from it;
header-correlated graphs without structural (SPAWN/TOOL/A2A) evidence are a
forest of independent runs.

## Tests

```
uv run pytest packages/otel_mapper -v
```

Fixtures in `testdata/` are full ExportTraceServiceRequest payloads covering
SPAWN, TOOL_DELEGATION, A2A (flag on/off), correlation-header membership, and
malformed input.
