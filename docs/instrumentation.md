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

## Framework adapters (e.g. LangGraph)

Two practical gotchas when wiring a real framework, learned the hard way:

- **Send JSON, not protobuf.** Python's stock `OTLPSpanExporter`
  (`opentelemetry-exporter-otlp-proto-http`) serializes to protobuf, which the
  ingest endpoint rejects with `400`. Either configure a JSON exporter, or use a
  small **collecting exporter** that buffers a run's spans and POSTs one
  `ExportTraceServiceRequest` as JSON on shutdown (hex `traceId`/`spanId`,
  key/value attributes — the shape in `packages/otel_mapper/testdata`).

- **Auto-instrumentation opens no runs by itself.** Framework auto-instrumentors
  (e.g. `openinference-instrumentation-langchain`) trace each graph node as a
  `CHAIN` span, not `AGENT` — and Agent Detective opens a run only for `AGENT`
  spans. Promote the real node spans to `AGENT` (set
  `openinference.span.kind = AGENT` + `gen_ai.agent.name`) so each node becomes a
  graph node *and* its child LLM spans roll their tokens/cost into it. Chaining
  the node spans in execution order (each node parented to the previous) turns
  the sequential flow into `SPAWN` edges; a `TOOL_DELEGATION` back-edge on a
  retry closes the cycle so the loop is detected. A collecting exporter is the
  natural place to apply all of this, since it sees the whole run at once.

- **Cost.** Agent Detective ships no pricing table, so `gen_ai.usage.cost` is
  whatever you set; if you only have token counts, compute cost = tokens × price
  in the exporter and attach it (again, absent → unknown, never a default).

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

**File artifacts must be embedded, not referenced.** A payload that only names
a produced file (`{"artifact_path": "report.docx"}`) gives every judge a
*description* of the work instead of the work: verifier verdicts over such
payloads are unverifiable, and the worker flags the node
`unverifiable_artifact` (capping its judge component). Instrument the exporter
to extract the artifact's text at flush time and append it to
`input.value`/`output.value` under an `artifact_text` marker — that marker is
what tells the scorer the content is actually visible. (The `generative_simon`
reference exporter does this for docx/md/html.)

### Edges

| Edge type | How it is detected |
|---|---|
| `SPAWN` | An `AGENT` span whose parent span belongs to a **different** agent's run. Edge points parent-run → child-run. Same-agent nested/retry AGENT spans emit no edge. |
| `TOOL_DELEGATION` | A `TOOL` span carrying `gen_ai.tool.target_agent`. Edge points **target → caller** (the target's output flows back into the run owning the tool span). |
| `A2A_MESSAGE` | An `a2a.task_id` attribute, or an HTTP client span whose path ends in `/.well-known/agent.json`, resolved via `a2a.peer_agent`. **Off by default** behind the `A2A_DETECTION` flag. |

Edges point in the direction of influence: `from_run`'s output feeds `to_run`.
That is the direction the blame engine expects — a node's predecessors explain
its quality. Every edge records which rule fired in `detection_method`.

## Deterministic signal & versioning conventions (universal)

These conventions are agent-agnostic: they are attribute names, not an SDK
requirement. The dependency-free helper package `packages/detective_sdk`
(pure stdlib, `pip install -e packages/detective_sdk`) computes the values for
**any** instrumentation — a LangChain exporter, an OpenAI-SDK wrapper, a custom
loop. `generative_simon` is merely the reference integration that consumes it
the same way a third-party agent would.

### The `agent_detective.artifact_meta` span attribute

The worker cannot open files — artifacts live on the instrumented host. The
exporter already opens them to embed `artifact_text`; it also computes a
deterministic integrity record per artifact and ships it **out-of-band as a
span attribute** on the same node spans (including the deliverable fallback
for terminal spans):

```
agent_detective.artifact_meta =
  [{"path":"report.docx","declared_ext":"docx","detected_kind":"zip",
    "nonempty":true,"parse_ok":true,"sha256":"ab12…","size":12345}]
```

A compact JSON **array** string, one entry per artifact path found in that
span's output. Ingest lands it verbatim in `agent_runs.artifact_meta`, and the
worker checks it into deterministic `artifact_integrity_fail` signals —
evidence that overrides LLM judgment, so a corrupt or mislabeled artifact is
caught without a judge call.

**Why an attribute and not a payload marker:** payload text is written by the
agent and can *contain document content* — content that could quote or forge an
integrity block, forcing a false deterministic `bad` on a healthy run or
masking a genuinely corrupt artifact. Span attributes are set by the exporter
alone; document content cannot inject them. The worker therefore never parses
integrity metadata out of payload text.

`detective_sdk.artifact_meta(path)` computes each entry: `detected_kind` from
magic bytes (`PK\x03\x04` → `zip`, `%PDF` → `pdf`, decodable UTF-8 → `text`,
zero bytes → `empty`, else `binary`; missing file → `missing`), `parse_ok` from
a format-appropriate open (docx/xlsx/pptx = zip opens and the main part exists,
pdf = header + `%%EOF`, md/txt/html/json = UTF-8 decode).

### Per-run identity attributes

Four attributes answer "why did this work yesterday?" by making every run
diffable. Set them as **span attributes on the `AGENT` span first**; resource
attributes are the fallback (the same span-attrs-then-resource rule as
`gen_ai.agent.name`):

| Attribute | Meaning | Helper |
|---|---|---|
| `gen_ai.agent.version` | the agent codebase version | `detective_sdk.git_version(repo_dir)` — short sha, `-dirty` suffix on uncommitted changes, cached per repo |
| `gen_ai.request.model` | the model identifier the run used | your model constant/config |
| `agent_detective.prompt_hash` | 12 hex chars of sha256 over the files that define the agent's prompts | `detective_sdk.content_hash(paths)` |
| `agent_detective.tool_schema_hash` | 12 hex chars of sha256 over the agent's tool JSON schemas (canonical sorted JSON — insensitive to key order and schema list order) | `detective_sdk.tool_schema_hash(schemas)` (constant: `detective_sdk.TOOL_SCHEMA_HASH_ATTRIBUTE`) |

All four are recorded per run and shown in the UI; diffing them between a
good graph and a bad one is the fastest deterministic answer to a regression.

### Control hook (opt-in)

Agent Detective **cannot stop anything** — it observes. When its worker
records an open circuit breaker for an agent, that decision sits in the
database and on the API (`GET /control/breakers`) until an integration
chooses to act on it. `detective_sdk.should_halt(endpoint, agent_name)` is
that opt-in: call it in your agent loop before doing work, and it returns
`True` only when the API positively reports an open breaker scoped to your
agent's name (`scope_kind = agent_name`). Everything else — connection
refused, timeout, a non-2xx response, unparseable JSON — returns `False`,
because observability must never take the agent down. An agent that never
calls the hook is completely unaffected; enforcement exists only where the
integration adds it.

## Limitations

- **A2A detection is feature-flagged — enabled by default in the reference
  compose.** `A2A_MESSAGE` edges are only produced when `A2A_DETECTION=true`
  (the mapper-level default stays `false`, per build spec 6.1, but
  `docker-compose.yml` now sets `A2A_DETECTION=true` on the ingest service;
  override with `A2A_DETECTION=false` in `.env`). Peer-to-peer / mesh
  architectures — agents exchanging `a2a.task_id`-correlated messages
  directly, e.g. market bid exchanges — **require** this flag: peer traffic
  crosses traces, so no SPAWN edge can ever link the peers, and the A2A rule
  is the only source of structure. Degraded mode with the flag off: the same
  interactions still land in one graph via the correlation header, but as
  membership without directed edges — blame localisation between the peers is
  impossible.
- **Correlation-header membership caveat.** `x-execution-graph-id` groups runs
  into one graph but conveys no edge direction (see above). Cross-process call
  structure must come from trace-context propagation or tool-delegation
  attributes.
- **Storage split.** ClickHouse stores the **raw spans** (table `otel_spans`,
  ordered by `(trace_id, start_time)`); Postgres stores the reconstructed
  **graph** (graphs, runs, edges, scores, incidents, blame reports). Large
  payloads that overflow the inline limit are stored in MinIO
  (bucket `agent-detective-payloads`); the inline column keeps a bounded prefix.
