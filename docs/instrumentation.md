# Instrumentation

Agent Detective is OTEL-native: there is no custom SDK and no proprietary
protocol. If your agents already emit OpenTelemetry traces with
[OpenInference](https://github.com/Arize-ai/openinference) or
[OpenLLMetry](https://github.com/traceloop/openllmetry) conventions, connecting
them is a matter of pointing their OTLP exporter at the ingest endpoint.

## One environment variable

The ingest service accepts **OTLP/HTTP JSON** at `POST /v1/traces` (default
port `8001`). Point your app's OTLP/HTTP exporter at it:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<ingest-host>:8001
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
```

The exporter appends `/v1/traces` to the endpoint itself, so set the base URL
only. Ingest consumes the JSON encoding of `ExportTraceServiceRequest`; make
sure the exporter is the HTTP/JSON one (`http/json`), not gRPC or protobuf.

That is the entire integration. Everything else — building the execution graph,
scoring nodes, and blame analysis — happens server-side from the spans you send.

## A concrete Python example

The demo pipeline in `demo/synthetic_pipeline/` is a working reference. It sets
the OpenInference span kind, the agent identity, token usage, cost, and the
input/output values that Agent Detective reads. The keys it uses are collected
in `demo/synthetic_pipeline/synthetic_pipeline/conventions.py`. A minimal
equivalent using `opentelemetry-sdk` plus the OpenInference semantic
conventions:

```python
from openinference.semconv.trace import (
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "my-agents"}))
provider.add_span_processor(
    BatchSpanProcessor(
        # Reads OTEL_EXPORTER_OTLP_ENDPOINT; or pass endpoint= explicitly.
        OTLPSpanExporter(endpoint="http://<ingest-host>:8001/v1/traces")
    )
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("my-agents")

with tracer.start_as_current_span("scraper.run") as agent_span:
    # This span *is* an agent run: it opens one node in the execution graph.
    agent_span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND,
                             OpenInferenceSpanKindValues.AGENT.value)  # "AGENT"
    agent_span.set_attribute("gen_ai.agent.name", "scraper-agent")
    agent_span.set_attribute("gen_ai.agent.version", "0.9.2")
    agent_span.set_attribute(SpanAttributes.INPUT_VALUE, "Scrape three product pages.")
    agent_span.set_attribute(SpanAttributes.OUTPUT_VALUE, scraper_output_json)
    agent_span.set_attribute("gen_ai.usage.input_tokens", 800)
    agent_span.set_attribute("gen_ai.usage.output_tokens", 220)
    agent_span.set_attribute("gen_ai.usage.cost", 0.006)
```

The demo builds its OTLP payload with a `SimpleSpanProcessor` and a custom
JSON exporter (`demo/synthetic_pipeline/synthetic_pipeline/exporter.py`) so it
can serialize deterministic fixtures; your production app should use the stock
`BatchSpanProcessor` + `OTLPSpanExporter` shown above.

**OpenLLMetry works too.** OpenLLMetry emits the same `gen_ai.*` usage
conventions and OpenInference-style span kinds, so the mapper reconstructs runs
and edges from its spans without any extra configuration. Any exporter that
sends OTLP/HTTP JSON with these attributes is compatible.

## The correlation header: `x-execution-graph-id`

By default Agent Detective groups a run into an execution graph by its trace id
(the single-trace assumption). When one logical execution spans **multiple
traces or multiple processes** — typically cross-process agent-to-agent (A2A)
calls where each service starts its own trace — set a shared correlation value
so all the runs land in one graph:

- as a span attribute `x-execution-graph-id`, or
- as the HTTP header attribute form
  `http.request.header.x-execution-graph-id`.

All runs carrying the same value are grouped into one execution graph.

### Limitation: membership only, not direction

The correlation header establishes graph **membership only**. It does **not**
imply edge direction. A shared header proves two agents participated in the same
execution; it does not say who called whom, and the mapper deliberately derives
**no edges** from it. Header-correlated runs with no structural edges are
treated as a forest of independent runs within the same graph.

Edge direction always comes from structure, never from the header:

- **SPAWN** — from span parentage (an `AGENT` span parented inside another
  agent's run).
- **TOOL_DELEGATION** — from the `gen_ai.tool.target_agent` attribute on a
  `TOOL` span.

If you need directed edges across processes, propagate the standard OTEL
trace context (so parentage survives), or use tool-delegation attributes — the
header alone will not reconstruct the call graph.

## What attributes matter

These are the exact keys `packages/otel_mapper` reads (build spec section 6.1).

### Agent runs

| Purpose | Attribute(s) |
|---|---|
| Opens a run (node) | `openinference.span.kind = AGENT` |
| Agent name / version | `gen_ai.agent.name`, `gen_ai.agent.version` (span attrs first, then resource attrs) |
| Input tokens | `gen_ai.usage.input_tokens` (fallback: `llm.token_count.prompt`) |
| Output tokens | `gen_ai.usage.output_tokens` (fallback: `llm.token_count.completion`) |
| Cost (USD) | `gen_ai.usage.cost` (the mapper ships no pricing table; absent → unknown) |
| Input / output payloads | `input.value` / `output.value` (OpenInference; non-strings are JSON-serialized) |
| Status | derived: `failed` if any member span reports an OTLP `ERROR` status, else `ok` |

Every span with `openinference.span.kind = AGENT` opens exactly one run. Every
other span (LLM, TOOL, etc.) is attributed to the run of its nearest
AGENT-ancestor within the same trace. Token/cost values on the AGENT span win;
otherwise they are summed over member spans; absent everywhere → unknown
(`None`, never a default). A missing score is treated as unknown all the way
through blame analysis — it is never assumed healthy.

### Edges

| Edge type | How it is detected |
|---|---|
| `SPAWN` | An `AGENT` span whose parent span belongs to a **different** agent's run. Edge points parent-run → child-run. Same-agent nested/retry AGENT spans emit no edge. |
| `TOOL_DELEGATION` | A `TOOL` span carrying `gen_ai.tool.target_agent`. Edge points **target → caller** (the target's output flows back into the run owning the tool span). |
| `A2A_MESSAGE` | An `a2a.task_id` attribute, or an HTTP client span whose path ends in `/.well-known/agent.json`, resolved via `a2a.peer_agent`. **Off by default** behind the `A2A_DETECTION` flag. |

Edges point in the direction of influence: `from_run`'s output feeds `to_run`.
That is the direction the blame engine expects — a node's predecessors explain
its quality. Every edge records which rule fired in `detection_method`.

## Limitations

- **A2A detection is off by default.** `A2A_MESSAGE` edges are only produced
  when `A2A_DETECTION=true` (default `false`, per build spec 6.1). With it off,
  A2A interactions still land in the same graph via the correlation header, but
  as membership without directed edges.
- **Correlation-header membership caveat.** `x-execution-graph-id` groups runs
  into one graph but conveys no edge direction (see above). Cross-process call
  structure must come from trace-context propagation or tool-delegation
  attributes.
- **Storage split.** ClickHouse stores the **raw spans** (table `otel_spans`,
  ordered by `(trace_id, start_time)`); Postgres stores the reconstructed
  **graph** (graphs, runs, edges, scores, incidents, blame reports). Large
  payloads that overflow the inline limit are stored in MinIO
  (bucket `agent-detective-payloads`); the inline column keeps a bounded prefix.
