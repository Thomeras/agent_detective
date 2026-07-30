# Instrumentation

Agent Detective is OTEL-native: no proprietary protocol, nothing to adopt in
your stack. If your agents already emit OpenTelemetry traces with
[OpenInference](https://github.com/Arize-ai/openinference) or
[OpenLLMetry](https://github.com/traceloop/openllmetry) conventions, connecting
them is a matter of pointing their OTLP exporter at the ingest endpoint.

**Have no instrumentation yet?** `detective-sdk` gives you a context manager per
agent step and emits exactly the same standard spans — see
[Nothing instrumented yet](#nothing-instrumented-yet-detective-sdk) below. It is
optional and dependency-free; you are never locked into it.

## Step by step

**1. Pick a receiver.** One run locally: `detective capture` (from
`pip install agent-detective`, listens on `127.0.0.1:8900`). Continuous
ingest: the compose stack (`docker compose up`, listens on `:8001`). Same
endpoint, same spans — nothing in your agent changes between them.

**2. Pick your path.**

- **A — already emit OTEL** (OpenInference / OpenLLMetry): two env vars and
  you are done:

  ```bash
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
  export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8900   # or http://<host>:8001
  ```

- **B — nothing instrumented yet**: `detective-sdk`, a context manager per
  step → [Nothing instrumented yet](#nothing-instrumented-yet-detective-sdk).

- **C — framework auto-instrumentation** (LangGraph, CrewAI, …): three lines
  with `detective_sdk.otel.collect` →
  [Framework adapters](#framework-adapters-eg-langgraph).

**3. Put the work in the payloads.** `output.value` must carry the step's
actual product (the document, the rows, the answer) — never a status record
like `{"ok": true}`; a judge handed a progress ping grades the ping.

**4. Verify.**

```bash
detective doctor run.json
```

It reports what the trace can and cannot support, with a concrete fix per
finding — before you trust any verdict built on it.

Everything below is the detail behind these four steps.

## Two receivers, one protocol

Both ends of Agent Detective speak the same endpoint, so instrumentation is
written once and works against either:

| Receiver | Command | For |
|---|---|---|
| `detective capture` | `pip install agent-detective` | one run, no stack, verdict in the terminal |
| ingest service | `docker compose up` | continuous ingest, incident inbox, cross-run history |

`detective capture` binds `127.0.0.1:8900` by default and prints the verdict
when the run ends; ingest listens on `8001` and persists. Point the same
exporter at whichever you want — nothing else in your app changes.

## One environment variable

The receiver accepts **OTLP/HTTP JSON** at `POST /v1/traces`. Point your app's
OTLP/HTTP exporter at it:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<ingest-host>:8001
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
```

The exporter appends `/v1/traces` to the endpoint itself, so set the base URL
only. Ingest consumes the JSON encoding of `ExportTraceServiceRequest`; make
sure the exporter is the HTTP/JSON one (`http/json`), not gRPC or protobuf.

That is the entire integration. Everything else — building the execution graph,
scoring nodes, and blame analysis — happens server-side from the spans you send.

### Already running an OpenTelemetry Collector?

Fan the same spans out to Agent Detective without touching your apps — add one
exporter to the collector config. Ingest accepts OTLP/HTTP in both wire
formats, so the stock `otlphttp` exporter (protobuf) works as-is:

```yaml
exporters:
  otlphttp/agent-detective:
    endpoint: http://<ingest-host>:8001

service:
  pipelines:
    traces:
      exporters: [<your-existing-exporter>, otlphttp/agent-detective]
```

Your existing tracing backend keeps receiving everything it already does;
Agent Detective just becomes one more consumer of the same stream.

## Nothing instrumented yet? `detective-sdk`

Everything above assumes you already emit OTEL spans. If you do not, the
conventions on this page are short but easy to get subtly wrong — and a wrong
guess is expensive, because the analysis stays confident while being wrong. A
span without `openinference.span.kind=AGENT` never becomes a node; a node whose
`output.value` holds a status record (`{"ok": true}`) gets its *phrasing* judged
instead of its work; an omitted cost is indistinguishable from a free run.

`detective-sdk` is that exporter, written once. Pure stdlib, zero dependencies —
instrumentation runs inside your agent's process, so it must not drag a judge or
a database in with it:

```python
from detective_sdk import run

with run("intel", task=user_request) as r:          # root carries the ORIGINAL ask
    with r.step("resolve") as s:                    # pipeline: parent = previous step
        s.output = company                          # the WORK, not {"ok": true}
    with r.step("collect") as s:                    # input defaults to resolve's output
        s.output = documents
        s.cost(usd=0.012, tokens_in=22_000, tokens_out=1_500, model="gpt-4o")
    with r.step("write") as s:
        s.output = dossier_markdown                 # deliverable text, not a file path
        s.artifact("out/dossier.md")                # integrity, outside the payload
```

Four shapes across five calls (a fan-out and its fan-in are one shape), because
topology changes how the graph reads:

| Call | Parent | Use for |
|---|---|---|
| `r.step(name)` | the previous step | pipelines / handoff chains |
| `r.span(name)` | the innermost open span | orchestrator trees, sub-agents |
| `r.branch(name)` | the step that dispatched | one arm of a fan-out — arms run beside each other, never chained into a pipeline nobody ran |
| `r.join(name, arms)` | the dispatch point | the fan-IN — one edge per arm, so blame walks back from a bad merge into the arm that poisoned it |
| `r.retry(name)` | the enclosing step | a loop — attempts get per-attempt identity (`write#1`, `write#2`) plus the back-edge that closes the cycle |

`join` and `retry` are constructs rather than conventions because a span carries
one `parentSpanId`: nesting can express a fan-OUT and never a fan-IN, and spans
sharing an agent name get no edge between them at all. Both emit attributes you
would otherwise have to learn from the mapper's source.

`step` also defaults each step's input to the previous step's output — that
handoff is what lets blame compare neighbours; without it every node looks like
it started from nothing.

`s.contract(lang="cs")` declares a parameter this step must not silently
rewrite. Read [Contracts](#contracts-the-agent_detectivecontract_params-attribute)
before writing your first one: the intuitive first attempt is a check that
cannot fire, and it fails by staying quiet.

Switched off unless `AGENT_DETECTIVE_ENDPOINT` or `AGENT_DETECTIVE_TRACE_FILE`
is set, so instrumented code ships to production untouched:

```bash
detective capture --once --out run.json        # terminal 1
AGENT_DETECTIVE_ENDPOINT=http://127.0.0.1:8900 python -m myagent   # terminal 2

# or skip the listener entirely
AGENT_DETECTIVE_TRACE_FILE=run.json python -m myagent && detective analyze run.json
```

Report only what you measured. `cost()` omits what you do not pass, and an
absent cost stays honestly unknown rather than becoming `$0` — otherwise a
metered agent looks more expensive than an unmetered one. Agent Detective ships
no pricing table and never infers a price.

### No `with` block? Start here, finish there

Existing code rarely lets you wrap a step in a `with` block. You hook whatever
callbacks the framework already fires — `on_chain_start` / `on_chain_end`, a
phase event bus, a graph-node listener — and open the step in one callback,
close it in another. Every construct has that event-driven half:

```python
from detective_sdk import run

r = run("pipeline", task=user_request)     # no `with`: the run outlives the function

def on_phase_start(phase):           r.step(phase)
def on_phase_finish(phase, result):  r.end(phase, output=result)
def on_phase_error(phase, err):      r.end(phase, output=str(err), failed=True)
def on_pipeline_finish():            r.close()        # the export happens HERE
```

| Opened by | Closed by |
|---|---|
| `r.step(name)` / `r.span(name)` / `r.branch(name)` | `r.end(name, output=…, failed=…)` — the most recent still-open step of that name |
| `loop.attempt(agent)` | `loop.end(agent, output=…, failed=…)` — the **plain** name (`"write"`), not `write#2`: the finish callback does not know which attempt it was |
| `r.retry(name)` | `loop.close(output)` |
| `run(...)` | `r.close()` |

Four properties that make this safe in a callback world:

- **A stray finish invents nothing.** `r.end(name)` returns `None` when no step
  of that name is open, instead of manufacturing a zero-length span.
- **A doubled finish is harmless.** `Span.end()` is idempotent, so a framework
  that fires "finished" twice cannot corrupt the timing.
- **`r.close()` is not optional.** The export happens there and nowhere else; a
  run that is never closed emits nothing. It is idempotent, so closing in both a
  finish hook and a `finally` is fine.
- **Off is still off.** With neither env var set every call above stays a cheap
  no-op, so the hooks ship to production untouched.

**A fan-out needs an arm registry.** `r.join` takes the arm spans, so the
dispatch callback has to keep them for the merge callback — this is the one
piece of bookkeeping the event-driven form adds:

```python
arms = {}                                  # arm name -> the open Span

def on_arm_start(name):            arms[name] = r.branch(name)
def on_arm_finish(name, result):   r.end(name, output=result)
def on_merge(result):              r.join("merge", list(arms.values())).end(result)
```

By merge time each arm has recorded its output, so the joiner's default input is
the real `{agent: output}` map rather than a claim about values it never saw.

**Phases must actually be sequential for `step`.** If your framework opens phase
B before phase A reports its finish, B is still parented on A but records **no
input** — the SDK will not substitute a handoff that did not happen. Concurrent
work is `branch`, not `step`.

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

### The short way: `detective_sdk.otel.collect`

All three gotchas below are mechanical, and all three fail *quietly* — you get
an empty or misshapen graph, never an error. `detective-sdk[otel]` ships them
solved, so an existing OTEL system connects in three lines instead of ~60 lines
of exporter glue:

```python
from detective_sdk.otel import collect

collect(
    endpoint="http://127.0.0.1:8900",            # or trace_file="run.json"
    promote=lambda s: s.name if s.name in NODES else None,   # CHAIN -> AGENT
    chain=True,                                  # sequential nodes -> SPAWN edges
    task=user_request,                           # optional run root (provenance)
)
```

`promote` also accepts a plain list of span names. The collector buffers the run
and sends one `ExportTraceServiceRequest` as JSON when the process exits —
promotion and chaining need the whole run, since you cannot re-parent node #4 to
node #3 in a batch that has not met node #3 yet.

Child LLM spans keep their original parent, which is what makes their tokens and
cost roll up into the node above them. Measured on a bridged vanilla-CrewAI-shaped
trace: three `CHAIN` nodes became a 4-node pipeline with 3 `SPAWN` edges and
per-node cost ($0.004 / $0.011 / $0.02), with the root honestly cost-unknown.

The rest of this section explains what `collect` does, for anyone writing the
exporter by hand.

### The gotchas it solves

Two practical gotchas when wiring a real framework, learned the hard way:

- **Protobuf and JSON both work.** Python's stock `OTLPSpanExporter`
  (`opentelemetry-exporter-otlp-proto-http`) serializes to protobuf; the ingest
  endpoint accepts it (`content-type: application/x-protobuf`) as well as the
  OTLP/JSON shape in `packages/otel_mapper/testdata` — both land on identical
  rows. Batch splitting is also safe: the finalizer re-maps the full stored
  span set before announcing the graph, so cross-batch edges and late root
  spans are recovered.

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

## Multi-stage and multi-process pipelines

One logical execution often lives in several processes or traces: a triage
service emits its own trace, the worker it dispatched to emits another. This
section is about getting those pieces into **one graph with the right edges**.

### Sharing graph identity across processes

By default every `run()` generates its own trace id, so two processes land in
two graphs. Pass the identity in from outside — the public parameters of
`run()`, never `run._trace_id`:

| Parameter | Effect |
|---|---|
| `trace_id=` | both processes emit spans under the **same trace id** — the strongest correlation, everything below works with no header at all |
| `parent_span_id=` | the downstream root span's parent — builds a structural edge across the process boundary |
| `graph_id=` | sets the `x-execution-graph-id` attribute on the root span — groups separate traces into one graph (see [the correlation header](#the-correlation-header-x-execution-graph-id) below) |

Hand the identity over through your own channel (a job payload, a queue
message); the SDK exposes exactly the two values you need to pass:

```python
# upstream process
with run("triage", task=request) as r:
    ...
    dispatch(job, trace_id=r.trace_id, parent=r.root_span_id)

# downstream process
with run("worker", task=job.task,
         trace_id=job.trace_id, parent_span_id=job.parent) as r:
    ...
```

### Two ways to build a cross-process edge

- **SPAWN via a propagated parent (preferred).** Passing `trace_id=` +
  `parent_span_id=` parents the downstream root span under the upstream one,
  and the mapper emits the SPAWN edge from structure alone — no name
  resolution, no ambiguity.
- **TOOL_DELEGATION resolved by name.** A `TOOL` span carrying
  `gen_ai.tool.target_agent = "worker"` yields an edge target → caller. The
  target run is resolved **by agent name**, and it does not have to share the
  caller's POST: at finalization ingest re-maps the full stored span set and
  resolves delegations deferred from earlier batches against all runs of the
  graph. Name resolution is inherently ambiguous, though — with two runs of
  the same agent name in one graph, the edge lands on the first match (same
  trace preferred, then earliest start, then run id). Prefer SPAWN whenever
  you control both ends.

### Limitations, written down

- **The correlation header gives membership, not direction.** A shared
  `x-execution-graph-id` puts both runs in one graph but yields no edge; edge
  direction always comes from structure. Full statement:
  [Limitation: membership only, not direction](#limitation-membership-only-not-direction).
- **A delegation names a role, not a run.** Two same-named targets = an edge
  to the first in resolution order. If you need a specific instance,
  propagate the parent span id instead.
- **The graph has a deadline.** Edges and identity are recomputed at
  finalization; whatever arrives after it is late — see below.

### The quiescence window — and what "late" means

Ingest keeps a graph open until its root run ends **or** no new spans arrive
for `GRAPH_QUIESCENCE_SECONDS` (default **30 s**; `FINALIZER_CHECK_SECONDS`
sets the scan cadence). At finalization the full stored span set is re-mapped
— that is when deferred delegations resolve and cross-batch edges appear —
and the graph is announced for analysis exactly once.

Verify the effective value instead of assuming the default: the ingest logs
it at startup (`effective config: graph_quiescence_seconds=…`) and serves the
non-secret subset on `GET /config`; `detective doctor` reads it from there
and says *unknown* when no ingest answers — it never assumes the default. For
pipelines whose stages start minutes apart, raise
`GRAPH_QUIESCENCE_SECONDS`, or the later stages land in a graph that was
already analyzed.

**Spans arriving after finalization are not silently dropped.** The graph row
records them (`late_spans_count`, `late_spans_last_at`), the ingest logs
them, and runs that arrive this way stay `not_analyzed`. Set
`REANALYZE_LATE_SPANS=true` to go further: the graph is re-mapped and
re-announced, triggering a fresh analysis over the now-complete span set. The
flag is off by default — a re-analysis can change a verdict consumers may
already have read.

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

**An empty payload is not a bad payload.** A span whose `output.value` is
absent — *or present but empty/whitespace* — leaves the node unscored
(`payload_missing`); it is never scored `0.0`. Orchestrator and wrapper spans
(LangGraph roots, CrewAI kickoff, a framework's top-level "run" span)
legitimately carry no output of their own, and treating emptiness as
"demonstrably bad" made them the culprit of every graph they appeared in — the
strongest possible verdict from the weakest possible evidence. If an empty
output genuinely *is* the defect, it reaches the report through the
deterministic channel (a failed status, a signal, a terminal verdict), not
through a quality number inferred from nothing. Non-root nodes in this state
raise an `instrumentation_warning` on the report — *"these nodes have no output
payload, fix the exporter"* — so the gap stays visible without being charged to
the agent.

**Unknown cost stays unknown.** With no `gen_ai.usage.cost` anywhere, a run's
cost is `null` — and it stays `null` through the graph total, the leaderboard,
and a blame report's `downstream_cost_usd`, rather than summing to a confident
`$0`. A partial sum (some nodes priced, some not) is reported as-is and is a
floor, not a total. Cost-ordered views place unmeasured agents last.

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

### Contracts: the `agent_detective.contract_params` attribute

The deterministic contract channel answers one narrow question: **did a
parameter that was supposed to survive this step come out different?** `lang=cs`
going in and `lang=en` coming out is a breach no judge is needed to confirm.

Normally both sides come from the payloads. The checker parses the input and the
output as JSON (or one `{...}` block embedded in prose), walks each recursively,
and compares every scalar it finds under a contract key. The built-in key set:

```
file_type, filetype, format, target_format, doc_kind, medium, lang, language, locale
```

When your payloads are prose or code, that walk has nothing to parse.
`agent_detective.contract_params` is the out-of-band lane — a flat JSON object
of scalars, set by instrumentation, standing in for the **input** side:

```
agent_detective.contract_params = {"lang":"cs","target_format":"pdf"}
```

In `detective-sdk` that is `span.contract(lang="cs", target_format="pdf")`.
Because instrumentation sets it and payload text cannot, a declared value
overrides whatever the input payload parse found for the same key. Declared keys
also widen the **output**-side search to themselves, so a key outside the
built-in list (`ico`, `tenant`, `currency`) is checked too.

#### The rule everything else follows from

**Declaring a parameter supplies the input side. The output side must still be
observable in the output payload — and it has to be the value the step
PRODUCED, not the value it was handed.**

A violation is reported only when a key appears on *both* sides with different
values (compared casefolded and unicode-normalized). That is the entire check,
and it is why the intuitive first contract usually cannot fire.

#### A contract that can never fire

```python
with r.step("fetch_registry") as s:
    s.contract(ico=ico)                        # input side: the ICO we asked for
    s.output = {"ico": ico, "name": name}      # output echoes the SAME variable
```

The `ico` in the output *is* the value that was declared, so `in == out` holds
by construction — on every run, including the ones where the step fetched the
wrong company entirely. The check is a tautology, and it announces nothing: an
unfalsifiable contract is indistinguishable from a satisfied one.

The same step, made falsifiable:

```python
with r.step("fetch_registry") as s:
    s.contract(ico=ico)                        # what must come back unchanged
    s.output = {"ico": record.ico, ...}        # the ICO the RECORD carries
```

Now the output side is evidence rather than an echo — a record for a different
company reports its own ICO, and the two values diverge.

Rule of thumb: **if the value in the output comes from the same expression you
passed to `contract()`, the pair proves nothing.** Wire one of the two to the
step's actual result, or drop the contract.

#### Three ways a contract stays silent

| Situation | Result | Why |
|---|---|---|
| Output is prose, or otherwise not parseable as JSON | no violation, ever | there is no output side to diff against |
| The declared key never appears in the output | no violation | declaring the input side does not conjure an output side |
| A key read **from a payload** carries two different values | dropped as **unknown** | a fan-in merging `lang=cs` and `lang=en` has no single contract to have violated; picking one would name a culprit on no evidence |

A declared value is never ambiguous in that last sense — it overrides the
input-side parse — but the *output* side is still subject to it, so a declared
key that appears twice in the output with different values also goes unknown.

Values must be scalars — nested objects, lists and `None` are ignored in the
declaration — and keys match case-insensitively. None of the three cases above
is an error, and none appears in the verdict. Silence is the designed
behaviour, which is exactly why a contract must be tested rather than assumed.

#### Test it by breaking it

`detective doctor` validates what the trace can support; it cannot tell whether
a declared contract is *falsifiable*. The check that works is the one you run
yourself — inject the violation you claim to be guarding against, and confirm
the verdict flips:

```python
s.contract(lang="cs")
s.output = {"lang": "en", "text": translated}   # deliberately wrong
```

If that run still comes back `PASSED`, the contract is wired to nothing. Do it
once per contract key before trusting the channel.

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
- **One trace file per run; `detective analyze` reads one file.**
  `AGENT_DETECTIVE_TRACE_FILE` is a single path, so a batch job must vary it per
  run — pass `trace_file=f"{outdir}/{run_id}.json"` to `run()` directly rather
  than relying on the env var. The analysis side already takes many at once, via
  the Python API rather than the CLI:

  ```python
  from detective_cli import analyze, bundles_from_exports, load_trace

  exports = [export for path in paths for export in load_trace(path)]
  result = analyze(bundles_from_exports(exports))
  ```

  Runs that belong to one logical execution still need a shared
  `x-execution-graph-id` (or propagated trace context) to land in one graph;
  being analysed in the same batch only makes cross-file edges *resolvable*.
- **Storage split.** ClickHouse stores the **raw spans** (table `otel_spans`,
  ordered by `(trace_id, start_time)`); Postgres stores the reconstructed
  **graph** (graphs, runs, edges, scores, incidents, blame reports). Large
  payloads that overflow the inline limit are stored in MinIO
  (bucket `agent-detective-payloads`); the inline column keeps a bounded prefix.
