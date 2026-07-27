# Using Agent Detective

One page that routes you through the whole product: instrumenting an agent,
analyzing its runs, and consuming verdicts from CI or another agent. Start
with the step-by-step; everything below it is the detail behind each step.

## Step by step: zero → verdict

**1. Install.**

```bash
pip install agent-detective
```

**2. Start a receiver** (terminal 1). It waits for one run, saves it, and
analyzes it the moment your agent finishes:

```bash
detective capture --once --out run.json
```

**3. Point your agent at it** (terminal 2) — pick one:

*Your agent already emits OpenTelemetry (OpenInference / OpenLLMetry):*

```bash
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8900
python -m your_agent
```

*Nothing instrumented yet — add `detective-sdk` (pure stdlib, zero deps):*

```python
from detective_sdk import run

with run("my-pipeline", task=user_request) as r:
    with r.step("write") as s:
        s.output = the_actual_result       # the work, not {"ok": true}
```

```bash
AGENT_DETECTIVE_ENDPOINT=http://127.0.0.1:8900 python -m your_agent
```

**4. Read the verdict.** Terminal 1 prints it when the run ends; exit code 1
means an incident. Re-analyze the saved trace any time:

```bash
detective analyze run.json --markdown   # findings brief for a coding agent
detective analyze run.json --json       # complete typed verdict
```

**5. Trust check.** Ask what this trace can actually support — every finding
comes with a concrete fix:

```bash
detective doctor run.json
```

**6. Optional — per-node LLM judge.** Off by default; without it nodes report
*unscored*, never silently "fine":

```bash
JUDGE_BASE_URL=http://localhost:11434/v1 JUDGE_MODEL=qwen2.5 \
  detective analyze run.json
```

**7. Optional — CI gate.**

```bash
detective analyze run.json --fail-on-unverified   # exit 1 on incident OR unmeasurable trace
```

**8. Optional — the full stack** (incident inbox, graph UI, cross-run history):

```bash
docker compose up --build     # web UI at :5173, ingest at :8001
```

---

That is the whole loop. The rest of this page is the detail, organized by
role:

| You are… | Start at |
|---|---|
| An agent developer who wants their agent's runs analyzed | [§1 Instrumenting your agent](#1-instrumenting-your-agent) |
| A developer/operator reading verdicts and running the stack | [§2 Analyzing runs](#2-analyzing-runs) |
| A coding agent or CI job consuming verdicts programmatically | [§3 Machine interface](#3-machine-interface--coding-agents--ci) |
| Looking up a flag, env var, or verdict label | [§4 Reference](#4-reference) |

---

## 1. Instrumenting your agent

Three paths, ordered by how much you already have. All three end at the same
place: OTLP/HTTP JSON spans arriving at `POST /v1/traces`.

### 1.1 Already instrumented (OpenInference / OpenLLMetry)

If your agents already emit OpenTelemetry traces with OpenInference or
OpenLLMetry conventions, there is nothing to adopt. Point the exporter at a
receiver:

```bash
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json     # not gRPC, not protobuf*
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8900   # detective capture
# or, against the full stack:
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<ingest-host>:8001
```

The exporter appends `/v1/traces` itself — set the base URL only.
\* The full-stack ingest service accepts protobuf too; `detective capture`
accepts JSON only and says so explicitly if you send protobuf.

What the mapper reads from your spans (the two that matter most):

- `openinference.span.kind = AGENT` — **only** these spans become graph nodes.
  A trace with only LLM/TOOL spans has no graph.
- `input.value` / `output.value` — must carry the **work** (the document, the
  rows, the answer), not a status record like `{"ok": true}`. A judge handed a
  progress ping grades the ping.

Full attribute and edge-detection tables: [instrumentation.md](instrumentation.md#what-attributes-matter).

### 1.2 Nothing instrumented yet — detective-sdk

`detective-sdk` is a pure-stdlib, zero-dependency package: a context manager
per agent step that emits exactly the standard spans above.

```bash
pip install agent-detective              # detective-sdk ships with it
# or, standalone in your agent's own environment (still zero dependencies):
pip install -e packages/detective_sdk
```

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
        s.artifact("out/dossier.md")                # integrity record, out-of-band
```

**Topology — pick the call that matches your control flow.** The shape of the
graph is what blame analysis reasons over, so use the right one:

| Call | Parent | Use for |
|---|---|---|
| `r.step(name)` | the previous step | pipelines / handoff chains |
| `r.span(name)` | the innermost open span | orchestrator trees, sub-agents |
| `r.branch(name, of=…)` | the fan-out point | parallel arms (never parents on a sibling arm) |
| `r.join(name, sources)` | — | fan-in; emits a read edge from every source |
| `r.retry(name)` | — | retry/reflection loops with per-attempt identity |
| `r.end(name, output=…)` | — | an explicit terminal step |

Fan-out / fan-in:

```python
with r.step("plan") as p:
    p.output = {"sections": ["intro", "specs", "pricing"]}
arms = []
for section in ("intro", "specs", "pricing"):
    with r.branch(f"write_{section}") as w:
        w.output = draft_section(section)
        arms.append(w)
with r.join("merge", arms) as m:                    # sources: Span objects or names
    m.output = merged_document
```

Retry loop — attempts get per-attempt identity (`write#1`, `write#2`, …) and
the loop's back-edge, which is what lets the analysis see a loop instead of a
line of disconnected nodes:

```python
with r.retry("revise_loop") as loop:
    for i in range(3):
        with loop.attempt("write") as a:
            a.output = draft
        with loop.attempt("qa") as a:
            a.output = verdict
    loop.output = final_draft
```

**Enriching a span** (all chainable, all optional):

| Method | What it records |
|---|---|
| `s.cost(usd=…, tokens_in=…, tokens_out=…, model=…)` | `gen_ai.usage.*` + model; omit what you did not measure — absent cost stays *unknown*, never `$0` |
| `s.artifact(path)` | deterministic integrity record (`agent_detective.artifact_meta`): size, hash, magic-bytes kind, parse check |
| `s.contract(file_type="pdf", …)` | declares carried contract parameters — enables the deterministic contract-breach check without payload conventions |
| `s.version(agent=…, prompt_hash=…)` | per-run identity for "why did this work yesterday?" diffs |
| `s.fail(reason=…)` | marks the span failed (OTLP error status) |
| `s.reads_from(other, tool="read")` | an extra inbound data edge |
| `s.attr(key, value)` | any raw OTLP attribute |

**Switching it on.** The SDK is a no-op unless one of these is set, so
instrumented code ships to production untouched:

```bash
detective capture --once --out run.json                            # terminal 1
AGENT_DETECTIVE_ENDPOINT=http://127.0.0.1:8900 python -m myagent   # terminal 2

# or skip the listener entirely:
AGENT_DETECTIVE_TRACE_FILE=run.json python -m myagent && detective analyze run.json
```

### 1.3 Existing OTEL framework (LangGraph, CrewAI, …) — `detective_sdk.otel.collect`

Framework auto-instrumentors trace nodes as `CHAIN` spans, not `AGENT` — and a
span that is not `AGENT` never becomes a node. `detective-sdk[otel]` ships the
fix as a three-line collector:

```python
from detective_sdk.otel import collect

collect(
    endpoint="http://127.0.0.1:8900",            # or trace_file="run.json"
    promote=["resolve", "collect", "write"],     # span names to promote CHAIN -> AGENT
    chain=True,                                  # sequential nodes -> SPAWN edges
    task=user_request,                           # optional run root (provenance)
)
```

`promote` also takes a callable `SpanRecord -> str | None`. Child LLM spans
keep their parent, so their tokens and cost roll up into the node above them.
Details and the by-hand version: [instrumentation.md](instrumentation.md#framework-adapters-eg-langgraph).

### 1.4 Verify before you trust: `detective doctor`

Bad instrumentation fails **silently** — a trace full of `{"ok": true}` still
produces a complete-looking, confident analysis. Run the doctor first:

```bash
detective doctor run.json          # or --json for the same diagnosis as data
```

It checks AGENT-span presence, agent names, edges and topology, the
deliverable node, payload presence and payload *content* (status-record
detection), `gen_ai.usage.*`, and artifact text vs. file reference — and then
tells you **what you can claim** from this trace (localization? cost? terminal
check?). Every finding states the consequence and a concrete fix. When it
cannot tell, it says `?` rather than guessing. It never gates: exit 0
whatever it finds.

### 1.5 Optional: the control hook

Agent Detective observes; it cannot stop anything. If you *want* your agent to
honor an open circuit breaker, opt in:

```python
from detective_sdk import should_halt

if should_halt("http://api-host:8000", "scraper-agent"):
    return  # breaker open for this agent; skip the work
```

Returns `True` only on a positively confirmed open breaker scoped to that
agent name; every failure mode (timeout, refused, bad JSON) returns `False` —
observability must never take the agent down.

---

## 2. Analyzing runs

### 2.1 Local mode — one trace, no infrastructure

```bash
pip install agent-detective
detective analyze run.json              # terminal report
detective analyze run.json --json       # complete typed verdict
detective analyze run.json --markdown   # findings brief for a coding agent
```

No database, no broker, no object store, and by default no LLM. Local mode
runs the **same** tier1/tier2 processors the deployed worker runs (imported,
not reimplemented) against in-memory seams, so a local verdict is comparable
to a deployed one. Where the deployed stack samples tier2, local mode analyzes
every graph.

To analyze a run that has not been captured yet, use `detective capture`
(§1.1/§1.2): it serves `/v1/traces`, waits for the export, then analyzes.

### 2.2 Turning on the per-node judge

The deterministic channel is always on. The judged channel needs a model —
any OpenAI-compatible endpoint:

```bash
JUDGE_BASE_URL=http://localhost:11434/v1 JUDGE_MODEL=qwen2.5 \
  detective analyze run.json            # e.g. local Ollama
```

With no judge configured, nodes report **unscored** rather than passing, and
the report says plainly that the judged channel was off. `--no-judge` forces
that mode even when the env vars are set. The judge is **role-aware**:
producer nodes are judged relative to their input; verifier nodes on whether
their PASS/FAIL verdict was correct.

### 2.3 Reading the report

The header line gives the verdict, the report type, and confidence:

```
FAILED  ·  cut_point  ·  confidence 62%
```

- **Origin vs. manifestation.** The report separates *where quality broke*
  (the first node whose score dropped from a healthy predecessor) from *where
  it surfaced* (the terminal output). Fixing the manifestation treats the
  symptom.
- **Confidence is a pair.** *Observation* (how sure the output is defective)
  and *attribution* (how sure this node is the origin) are reported
  separately, with structural caps — a fallback verdict is never sold as
  certainty.
- **Defects carry evidence.** Each defect cites its supporting findings
  (rule fingerprints, judge verdicts, requirement quotes) and its caveats
  (`base_assumed`, `observability_boundary`, `unverified_in_channel`,
  `recovered`) as fields, not prose.
- **The pipeline table** shows every node: score, ORIGIN/surfaced markers,
  verifier role, score drops, and *why* unscored nodes are unscored.
- **Unmeasured is not fine.** A graph nothing could be measured on renders
  `NOT VERIFIED` — an absence of evidence, not evidence of correctness.

Verdict labels and report types: [§4.3](#43-verdicts-and-report-types).

### 2.4 The full stack — continuous ingest, inbox, history

```bash
docker compose up --build                 # infra + ingest + worker + api + web
./demo/run.sh                             # happy-path demo: graph, no incident
./demo/inject_fault.sh && ./demo/run.sh   # next run carries an injected fault
```

No `.env` needed; the bundled mock LLM acts as the judge, so the stack runs
with no external API keys. Port collisions? `docker compose -f
docker-compose.yml -f docker-compose.altports.yml up -d` remaps everything.

| Service | Port | What |
|---|---|---|
| web UI | 5173 | incident inbox, graph list, graph view, leaderboard |
| read API | 8000 | graphs, incidents, blame reports, agent stats |
| ingest | 8001 | `POST /v1/traces` (JSON **and** protobuf) |
| mock LLM | 8080 | OpenAI-compatible judge stand-in |
| ClickHouse / Postgres / Redis / MinIO | 8123 / 5432 / 6379 / 9000–9001 | raw spans / graphs+reports / streams / payload overflow |

**The web UI** (`http://localhost:5173`): incident inbox → graph view with
defect cards (evidence / counter-evidence / context, observation+attribution
meters, caveat chips), graph canvas with the culprit ring, per-run versions
and raw evidence, and the `Export .md` button — the same findings brief as
`--markdown`. The leaderboard ranks agents across runs (group by version to
see regressions); unmeasured agents sort last, never as free/perfect.

**The read API** (`http://localhost:8000`) — the endpoints you will actually
use:

| Endpoint | What |
|---|---|
| `GET /graphs`, `GET /graphs/{id}` | list / full graph with runs, edges, reports |
| `POST /graphs/{id}/analyze` | queue a (re-)analysis |
| `GET /incidents`, `GET /incidents/{id}`, `PATCH /incidents/{id}` | inbox, detail with latest blame report, status change |
| `GET /agents/leaderboard?group_by=version` | cross-run agent stats |
| `GET /agents/{name}/versions/compare?base=&candidate=` | version-to-version diff |
| `GET /calibration` | judge-vs-human-label calibration |
| `GET /control/breakers` | what `should_halt` polls |
| `GET /audit/verify/{report_id}` | HMAC audit verification of a report |

**Production notes.**

- Set `AUDIT_HMAC_KEY` (api **and** worker, same value) — the default is
  `dev-insecure-key`.
- `TIER2_SAMPLE_PCT` controls how many un-flagged graphs get full per-node
  scoring (flagged graphs always do).
- `A2A_DETECTION=true` (compose default for ingest) enables cross-trace
  agent-to-agent edges; peer/mesh architectures require it.
- One logical execution across multiple traces/processes? Correlate with the
  `x-execution-graph-id` attribute — membership only, direction still comes
  from structure ([instrumentation.md](instrumentation.md#the-correlation-header-x-execution-graph-id)).

---

## 3. Machine interface — coding agents & CI

Everything in this section is stable surface intended for automation.

### 3.1 Exit codes

| Code | Meaning |
|---|---|
| 0 | analysed, no incident |
| 1 | at least one incident |
| 2 | analysis could not run (unreadable file, no AGENT spans) |

Modifiers: `--exit-zero` always exits 0 (report, don't gate);
`--fail-on-unverified` also exits 1 when a graph could not be measured at all
(`NOT VERIFIED`). `detective doctor` is deliberately not a gate: always exit 0.

### 3.2 Gate a build

```yaml
# CI step: fail the job when the agent run ships a defect
- run: |
    AGENT_DETECTIVE_TRACE_FILE=run.json python -m myagent
    detective analyze run.json --fail-on-unverified
```

`--fail-on-unverified` is the honest strictness knob: without it, a trace that
captured nothing measurable passes silently.

### 3.3 The Markdown findings brief

```bash
detective analyze run.json --markdown > findings.md
```

A self-contained brief — verdict, origin, defects with evidence, per-node
notes — written to be handed to a coding agent to drive the fix. The web UI's
`Export .md` produces the same document for any graph.

### 3.4 JSON output

`--json` emits the complete typed verdict:

```json
{
  "source": "run.json",
  "judge": {"enabled": false, "description": "not configured"},
  "incidents": 1,
  "graphs": [{
    "graph_id": "…", "graph_type": "…", "run_count": 5,
    "verdict": "FAILED", "measured": true,
    "report_type": "cut_point", "confidence": 0.62,
    "culprits": ["translator"], "culprit_run_ids": ["…"],
    "incident": {"key": "…", "trigger": "…"},
    "tier1": {"terminal_verdict": "bad", "flags": ["…"], "flagged": true},
    "tier2_ran": true,
    "evidence": { "findings": ["…typed, with provenance…"], "defects": ["…"] }
  }]
}
```

`evidence` carries the full schema-2 verdict: typed findings with provenance,
defects with origin sum type and referenced evidence, candidacy records,
per-defect confidence breakdown.

### 3.5 Golden replay — `detective-ci`

Deterministic regression gate for the analysis itself: record a fixture's
verdict once, fail CI when it drifts.

```bash
python -m detective_ci record fixture.json golden.json
python -m detective_ci check  fixture.json golden.json   # exit 1 on regression
```

Or as a pytest plugin (installed with `detective-ci`): the `detective_golden`
fixture offers `load_fixture`, `record`, `assert_matches_golden`, and
`stable_surface`. Example fixture/golden pairs: `packages/detective_ci/examples/`.

### 3.6 Library use

```python
from pathlib import Path
from detective_cli import analyze, bundles_from_exports, load_trace

run = analyze(bundles_from_exports(load_trace(Path("run.json"))))
for graph in run.incidents:
    print(graph.blame_report["report_type"], graph.blame_report["culprit_run_ids"])
```

The blame engine itself (`blame-engine`) is a pure, I/O-free package over
networkx graphs, usable without any of the CLI or services.

---

## 4. Reference

### 4.1 CLI

```
detective analyze <trace>    # analyze an OTLP/HTTP JSON trace file
detective capture            # serve POST /v1/traces, then analyze what arrives
detective doctor <trace>     # instrumentation diagnostic (never gates)
detective --version
```

`analyze` accepts a single `ExportTraceServiceRequest` object, a JSON array of
them, or JSON-lines.

Shared options (`analyze` and `capture`):

| Flag | Effect |
|---|---|
| `--json` / `--markdown` | machine verdict / findings brief (mutually exclusive) |
| `--no-judge` | deterministic channel only, even if a judge is configured |
| `--tier1-only` | cheap detection pass only — no per-node scoring or blame |
| `--a2a` | enable agent-to-agent edge detection in the mapper |
| `--exit-zero` | report without gating |
| `--fail-on-unverified` | exit 1 also on `NOT VERIFIED` graphs |
| `--color auto\|always\|never` | terminal color (respects `NO_COLOR`) |
| `-v` / `--verbose` | pipeline logs to stderr |

`capture` options:

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | loopback only; pass `0.0.0.0` deliberately (e.g. containers) |
| `--port` | `8900` | listen port |
| `--out PATH` | — | also save the received trace |
| `--once` | — | stop after the first export arrives |
| `--no-analyze` | — | capture only (requires `--out`) |

`doctor` options: `--json`, `--a2a`, `--color`.

### 4.2 Environment variables

**Your agent's side (exporter / SDK):**

| Variable | Meaning |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | receiver base URL (`http://127.0.0.1:8900` capture / `http://host:8001` ingest) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | must be `http/json` for `detective capture` |
| `AGENT_DETECTIVE_ENDPOINT` | detective-sdk: POST target; unset = SDK is a no-op |
| `AGENT_DETECTIVE_TRACE_FILE` | detective-sdk: write the trace to a file instead/as well |
| `AGENT_DETECTIVE_SERVICE_NAME` | detective-sdk: `service.name` resource attribute (→ `graph_type`) |

**The judge (local mode and worker):**

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_BASE_URL` | — (off) | any OpenAI-compatible `/v1` base URL |
| `JUDGE_MODEL` | `judge` | model name sent to the endpoint |
| `JUDGE_API_KEY` | `sk-none` | bearer token if the endpoint needs one |
| `JUDGE_TIMEOUT_S` / `JUDGE_CONCURRENCY` / `JUDGE_MAX_TOKENS` | `30` / `4` / `1024` | client tuning |
| `JUDGE_SEED` | unset | forwarded as `seed`; best-effort determinism |

**Full stack:** all knobs have working defaults in `docker-compose.yml`
(`${VAR:-default}`); `.env.example` documents them. The ones that matter
first: `AUDIT_HMAC_KEY`, `TIER2_SAMPLE_PCT`, `A2A_DETECTION`,
`GRAPH_QUIESCENCE_SECONDS`, `PAYLOAD_INLINE_MAX_KB`.

### 4.3 Verdicts and report types

| Verdict label | Report types behind it |
|---|---|
| `FAILED` | `cut_point`, `multi_culprit`, `composition_failure`, `loop_detected`, `verification_gap`, `root_cause_external`, `terminal_defect_unlocalized` |
| `LATENT DEFECT` | `shipped_with_latent_defect` — a silent defect shipped in the deliverable |
| `PASSED — with warnings` | `degraded_recovered` — quality dropped mid-graph but recovered |
| `INCONCLUSIVE` | `unclassified` — measured, but no typed defect explains it |
| `PASSED` | measured, clean |
| `NOT VERIFIED` | nothing could be measured — absence of evidence, not correctness |
| `UNANALYSED` | analysis never ran on this graph |

| Report type | One line |
|---|---|
| `cut_point` | quality demonstrably broke at one localized origin |
| `multi_culprit` | independent origins in separate branches |
| `verification_gap` | a verifier (`qa`/`eval`) passed bad work |
| `loop_detected` | a retry/reflection loop is implicated |
| `root_cause_external` | the fault entered from outside the graph (bad input) |
| `composition_failure` | every node locally fine; the composed result is not |
| `degraded_recovered` | a mid-graph drop that recovered before the terminal |
| `shipped_with_latent_defect` | the deliverable carries a defect terminal checks did not surface |
| `terminal_defect_unlocalized` | the output is defective; the origin could not be pinned |
| `unclassified` | measured, unexplained |

Incident severity (worst first): `latent_defect` > `degraded_quality` >
`terminal_failure` > `cost_overrun`.

### 4.4 Packages and distributions

| Distribution | Import | What | License |
|---|---|---|---|
| `agent-detective` | `detective_cli` | the `detective` CLI + local mode — `pip install agent-detective` | BSL 1.1 |
| `detective-sdk` | `detective_sdk` | zero-dependency instrumentation; ships as a dependency of `agent-detective`, or install from the repo (`[otel]` extra for the collector) | BSL 1.1 |
| `detective-ci` | `detective_ci` | golden replay + pytest plugin; install from the repo | BSL 1.1 |
| `blame-engine` | `blame_engine` | pure blame analysis over networkx | BSL 1.1 |
| `otel-mapper` | `otel_mapper` | OTLP span → run/edge mapping | Apache-2.0 |
| `agent-detective-worker` | `worker` | the tier1/tier2 pipeline (`[server]` extra for deployment) | BSL 1.1 |

Wheels are built per distribution: `uv build --package <name>` (see the root
README's Development section).

More depth: [instrumentation.md](instrumentation.md) (attributes, adapters,
correlation), [architecture.md](architecture.md) (system design),
[capabilities.md](capabilities.md) (what it can claim today),
[trace-requirements.md](trace-requirements.md) (claim → required capture),
[deterministic-signals.md](deterministic-signals.md) (the signal catalogue).
