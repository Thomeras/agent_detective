# Architecture

## The wedge

Multi-agent systems fail quietly. One agent hallucinates a fact, every
downstream agent faithfully carries it forward, every span reports
`status = ok`, and the final output is confidently wrong. Generic tracing shows
you the spans; it does not tell you **which node broke the quality**.

Agent Detective's wedge is exactly that: find the first node in the agent
execution graph where quality broke — the culprit — then follow the propagation
path to the final output and total the downstream cost of the failure. The
stance is OTEL-native (standard OpenTelemetry traces in, no custom SDK) and
graph-first (the execution graph, not a flat span list, is the primary data
model).

## System diagram

```
                         OTLP/HTTP JSON
   instrumented agents ───────────────────▶  services/ingest
   (OpenInference /                          POST /v1/traces
    OpenLLMetry)                                  │
                                                  │  1. raw spans
                                                  ├──────────────▶ ClickHouse
                                                  │                (otel_spans)
                                                  │  2. otel_mapper: spans
                                                  │     -> runs + edges
                                                  ├──────────────▶ Postgres
                                                  │                (graphs, runs,
                                                  │                 edges, scores)
                                                  │  3. payload overflow
                                                  └──────────────▶ MinIO
                                                                   (agent-detective
                                                                    -payloads)
                                                  │
                        finalizer (quiescence /   │
                        ended root span)          ▼
                                          Redis: ad.graphs.completed
                                                  │
                                                  ▼
                                   services/worker  tier1  (group: tier1)
                                   cheap always-on checks + ONE terminal judge
                                                  │  flagged or sampled
                                                  ▼
                                          Redis: ad.graphs.tier2
                                                  │
                                                  ▼
                                   services/worker  tier2  (group: tier2)
                                   per-node scoring + blame_engine.find_blame()
                                                  │
                                                  ├──▶ Postgres: incidents,
                                                  │            blame_reports
                                                  ▼
                                          Redis: ad.incidents.created
                                                  │
                                                  ▼
                                   services/worker  alerter (group: alerters)
                                   Slack webhook (or console log); alerts on
                                   is_new only
        ┌─────────────────────────────────────────┘
        ▼
   services/api (FastAPI, reads Postgres) ──────▶ web (React + cytoscape.js)
```

Also present: the `POST /graphs/{id}/analyze` API endpoint and an optional
sampler can publish to `ad.graphs.tier2` directly (bypassing tier1), and an
`XAUTOCLAIM` reaper moves poison messages to `<stream>.dlq` after
`MAX_DELIVERIES` failed deliveries.

## Two-tier detection

Full per-node scoring runs an LLM judge on every node and is expensive. Running
it on every graph would not scale, but skipping it would miss the flagship
failure mode — the silent hallucination where every status is `ok`. The split
resolves this:

- **Tier 1** (`worker/tier1.py`, stream `ad.graphs.completed`, group `tier1`) is
  cheap and always-on. It runs deterministic flags (failed runs, cost over
  budget, loop anomaly quick-check, schema violations, degenerate/empty terminal
  output) plus **one** LLM judge call on the *terminal* output — the graph's
  goal, its input, and the full (deterministically truncated) terminal output.
  That single judge call is what catches a silent hallucination despite every
  span reporting success. Tier 1 upserts a `tier1_verdicts` row (PK `graph_id`,
  idempotent). If flagged, it promotes the graph to `ad.graphs.tier2`;
  otherwise it samples at `TIER2_SAMPLE_PCT` (default 0; the demo uses 100).

- **Tier 2** (`worker/tier2.py`, stream `ad.graphs.tier2`, group `tier2`) is
  expensive and runs only on flagged or sampled graphs. It claims the job via
  `tier2_jobs (dedup_key)` `ON CONFLICT DO NOTHING` for idempotency, scores
  every node (`worker/scoring.py`: schema / judge / heuristics / **contract**
  components, weight-renormalized, judge run per-node relative to that node's
  input), runs the blame engine, enriches the report with post-blame fact
  propagation, and persists incidents and blame reports.

Because tier1's terminal judge sees the *full* terminal output (not a summary),
it does not throw away the very facts that distinguish a hallucination — one of
the design defects the revised spec fixes.

### Role-aware judging

The per-node judge is **role-aware** (`worker/prompts/`): *producer* nodes are
judged on the correctness of their transformation *relative to the input they
received* (`judge.md`, with explicit calibration anchors so a critical reasoning
never lands at 0.8), while *verifier / gate* nodes — `qa`, `eval`, `review`, … —
are judged on the **correctness of their PASS/FAIL verdict** (`judge_verifier.md`).
Without this split a verifier that rubber-stamps broken work reads as "healthy"
and the engine rewards the liar; with it, a rubber-stamper scores low (its
verdict was wrong) while an honest whistle-blower stays high.

### Deterministic input-contract check

Before the judge, `worker/scoring.py` runs a **contract** component: a node that
silently rewrites a carried-through parameter (`file_type`, `lang`, `format`, …)
between its input and output is a hard fault, detectable without any LLM. A
confirmed violation forces the node below threshold, so it surfaces as a real
`cut_point` culprit with concrete evidence rather than hiding behind a fluent
judge — the archetypal "first node that broke quality".

The judge is any OpenAI-compatible chat endpoint (`JUDGE_BASE_URL` /
`JUDGE_MODEL` / `JUDGE_API_KEY`): the bundled mock LLM, a local Ollama, or a
hosted model (e.g. OpenRouter). A weak model that cannot reliably emit the
`{task_score, input_flawed, reasoning}` JSON leaves nodes unscored, so a reliable
instruct model is recommended for production scoring.

## Blame engine

`packages/blame_engine/` is pure Python (networkx only), I/O-free and
unit-tested. Public entry point: `find_blame(BlameInput) -> BlameReport`. The
build spec section 3 is the authoritative description; the summary:

1. **SCC condensation** (`condense.py`). The graph may contain cycles
   (retry/reflection/orchestrator↔worker loops are normal traffic), so it is
   condensed into a DAG of strongly-connected components. A super-node's score
   is the score of its **exit node** (the last-finished member) — the value that
   actually flows downstream — with `min_member_score` kept only as evidence.
   Cycles never raise and never by themselves classify a failure.

2. **Loop-anomaly detection** (`loops.py`). A super-node with more than one
   iteration is anomalous only when it exceeds `max_loop_iterations`, or when a
   statistical baseline for the dominant agent (`sample_count >=
   loop_min_history`) puts its iteration count beyond `mean + loop_zscore*std`.
   Benign loops produce nothing.

3. **Edge-drop origins** (`cutpoint.py`). The cut point is where quality
   *broke*, not merely where it is low. An **origin** is a node whose score
   dropped past `gap_threshold` from a **healthy** (`>= threshold`), *observed*
   predecessor — quality was fine going in and broke here — or a degraded
   **boundary** (a source, or a node with only unknown predecessors) that is not
   immediately cured by a healthy successor. This is deliberate: a low *source*
   whose successors recovered is a spurious low (the "blame the orchestrator
   instead of the node that actually broke" bug), and only becomes the culprit
   when no real downstream drop-origin exists. A node that faithfully processed
   already-`input_flawed` work is a propagation point, never an origin.
   **Shadowing** keeps only the earliest origin on each branch.

4. **Confidence** (`confidence.py`) blends drop magnitude, severity, and
   predecessor quality, applies penalties (multi-member SCC, multi-culprit) and
   the unknown-ancestor cap — then a **per-report-type honest-confidence
   ceiling** (`blame.py::_CONFIDENCE_CAP`): `composition_failure` ≤ 0.4,
   `root_cause_external` ≤ 0.5, `multi_culprit` ≤ 0.8, `verification_gap` ≤ 0.6.
   Only `cut_point` (a real gap) and `loop_detected` (a deterministic breach)
   keep full confidence. A fallback verdict must never be sold as certainty.

5. **Classification** (`blame.py`, first match wins) yields one of seven report
   types:

   | report_type | when |
   |---|---|
   | `unclassified` | all scores unknown, or nothing else matched |
   | `root_cause_external` | no in-graph origin, but a source reports `input_flawed = True` (the fault entered from outside) |
   | `loop_detected` | an anomalous loop is the culprit (or there are no origins); culprits are its members |
   | `cut_point` | exactly one origin — a single-edge drop past `gap_threshold`, **or** a cumulative degradation chain (≥ `cum_min_edges` consecutive declining edges totalling ≥ `cum_drop_threshold`; the first eroding node is the origin, the whole chain lands in `evidence.degradation_paths`) |
   | `multi_culprit` | more than one independent origin (parallel branches) |
   | `verification_gap` | verifiers whose PASS was wrong while no producer origin localised. Two detection routes: the role-aware judge scored the verdict itself wrong (`basis=verdict_scored_incorrect`), or — deduced — the terminal verdict is bad and the verifier let the work through with a healthy score (`basis=passed_bad_terminal`). The second route keeps the engine honest even when the judges are blind (e.g. a file artifact nobody opened). |
   | `composition_failure` | no origin, terminal verdict bad, all scored nodes healthy, no single-edge drop **and no cumulative degradation chain**, no passing verifiers to blame, no *hidden* unknowns — suspect is the orchestration/design *layer* (shown as such in the UI), entering at the source |

**Loop drill-down.** When a `cut_point` origin is a multi-member SCC (a retry
loop), the blame is drilled into the **worst-scoring member** — where quality
actually broke inside the loop — with its real drop against its own raw
predecessor, not the loop's exit node that merely carried the failure downstream.

The report also carries: the **propagation path** (culprit → terminal, SCCs
expanded by end-time); the **downstream cost** (culprits' cost plus all
descendants, deduplicated); **origin vs manifestation** (`manifestation_run_ids`
— the terminal *artifact/output* the failure surfaced in; a verifier sink is
mapped back to the producer whose work it judged, because a verdict is not a
manifestation); the **terminal-verdict evidence** (`evidence.terminal_verdict`
— the tier1 judge's bad/ok + score + reasoning, so a claim like "terminal is
bad" is never made without showing its evidence, and an explicit
`verdict_conflict` note whenever a healthy-scored sink contradicts it);
**verification gaps** (with their detection `basis`); **degradation paths**
(cumulative erosion chains); every significant **drop** (> 0.2, not only
culprits); `topo_order` + `verifier_run_ids` (so the UI renders the score map
in pipeline order with verifiers grouped apart); per-node structured
**flags** from scoring (e.g. `unverifiable_artifact`, `missing_required_content`
— each deterministically caps the judge component, so a judge whose reasoning
admits a defect cannot keep a "good"-band score); and a per-node **candidacy
trace** carrying the actual numbers (score vs threshold, drop vs reference,
exclusion reason) so the verdict is auditable rather than a black box.

See `packages/blame_engine/blame_engine/` for the exact algorithm; the module
split mirrors the steps above.

## Data model

Reconstructed graph state lives in Postgres (Alembic, one initial migration —
build spec section 5). High-level tables:

| Table | Holds |
|---|---|
| `execution_graphs` | one row per graph: type, status (`active`/`finalized`), timing, total cost, run count |
| `agent_runs` | one row per node: agent name/version, parentage, status, inline/overflow input+output, `quality_score`, `score_components`, `unscored_reason`, `input_flawed`, tokens, cost |
| `edges` | directed edges with `type` (`SPAWN`/`A2A_MESSAGE`/`TOOL_DELEGATION`) and `detection_method`; unique on `(graph, from, to, type)` |
| `tier1_verdicts` | one row per graph (PK): terminal judge verdict/score/reasoning, deterministic flags, flagged/sampled |
| `tier2_jobs` | claim table: `dedup_key` unique, trigger, status; enforces idempotent tier2 |
| `incidents` | one per `(graph_id, incident_key)`; trigger category and status |
| `blame_reports` | versioned reports (`is_latest`), report_type, culprits, propagation path, confidence, downstream cost, evidence JSONB |
| `agent_stats` | per-agent token/iteration baselines used by scoring and loop detection |
| `output_contracts` | registered JSON schemas for schema-component scoring |
| `checkpoints` | prepared for replay (out of MVP scope) |

Raw spans live in ClickHouse (`otel_spans`, `ORDER BY (trace_id, start_time)`).
Payload overflow lives in MinIO (bucket `agent-detective-payloads`, keys
`payloads/{graph_id}/{run_id}/{input|output}`); the inline Postgres column keeps
a bounded prefix.

## Stream topology

| Stream | Producer | Consumer group | Purpose |
|---|---|---|---|
| `ad.graphs.completed` | ingest finalizer | `tier1` | cheap always-on detection |
| `ad.graphs.tier2` | tier1, API (`/analyze`), sampler | `tier2` | full scoring + blame |
| `ad.incidents.created` | tier2 | `alerters` | Slack/webhook notification |
| `<stream>.dlq` | `XAUTOCLAIM` reaper after `MAX_DELIVERIES` | manual | poison messages |

Messages are JSON with `schema_version: 1`. `XACK` happens only after the
Postgres transaction commits; unique constraints make any redelivery a no-op.

## Local mode (the `agent-detective` pip distribution)

The same analysis runs two ways, and the difference is deliberately confined to
four seams. `packages/detective_cli` imports `Tier1Processor` and
`Tier2Processor` — it does not reimplement or approximate them — and hands them
in-process implementations of everything they talk to:

| Seam | Deployed | Local (`detective analyze`) |
|---|---|---|
| `Repo` | Postgres (`worker/pg.py::PgRepo`) | `worker/memory.py::InMemoryRepo` |
| `ObjectStore` | MinIO | inline payloads (`InMemoryObjectStore`) |
| `StreamPublisher` | Redis Streams | `CollectingPublisher` (records the handoff) |
| `JudgeClient` | OpenAI-compatible endpoint | the same, or `NullJudge` |

Consequences worth stating, because they are what make the two comparable:

- `InMemoryRepo` reproduces the **idempotency semantics** the SQL provides
  (job claim, `(graph_id, incident_key)` uniqueness, blame-report versioning,
  the evidence hash chain via the shared `ledger_entry`) — it is production
  code, and the worker's own test suite runs against it, so the fake cannot
  drift into merely agreeing with itself.
- The tier1→tier2 **handoff still goes through a published message**; local
  mode reads what tier1 published instead of deciding for it. Only the
  *sampling* input differs (`tier2_sample_pct` defaults to 100 locally —
  analysing one file on purpose is not the same situation as a percentage of
  production traffic). Every engine threshold keeps its deployed value.
- `worker.repository` holds only the `Repo` protocol and the pure helpers, so
  importing the pipeline pulls no database driver. The database, broker and
  object-store clients live behind the distribution's `[server]` extra;
  `redis`, `minio` and `httpx` are additionally imported lazily inside their
  own constructors.

What local mode cannot do is anything that needs the deployment's state: the
registered rules/contracts registry is empty (so registered required-section
and JSON-schema checks are inert), there are no cross-run agent baselines to
compare against, and no incident history to supersede.

## Component map

| Path | Component |
|---|---|
| `packages/blame_engine/` | pure, I/O-free blame analysis (networkx only) — **BSL 1.1** |
| `packages/otel_mapper/` | OTLP/HTTP JSON span → run/edge mapping, plus the shared uuid5 run/graph id derivation (`ids.py`) — **Apache-2.0** |
| `packages/detective_cli/` | the `agent-detective` pip distribution: `detective analyze`, the in-process tier1→tier2 run, terminal/Markdown/JSON renderers |
| `packages/detective_sdk/` | zero-dependency instrumentation helpers (artifact meta, version/prompt hashes, opt-in halt hook) |
| `packages/detective_ci/` | deterministic golden replay of a blame fixture + pytest plugin (CI gate on the stable surface) |
| `services/ingest/` | OTLP/HTTP trace ingest, ClickHouse/Postgres/MinIO writes, finalizer |
| `services/worker/` | tier1/tier2 pipeline over Redis Streams, judge client, alerter, DLQ reaper |
| `services/api/` | FastAPI read API: graphs, incidents, blame reports, agent leaderboard, manual analyze |
| `web/` | React + Vite + TypeScript + cytoscape.js UI (dark-mode-first): incident inbox, graph list, loop-aware graph view, agent leaderboard, Markdown findings export (`findingsExport.ts`) |
| `demo/mock_llm/` | OpenAI-compatible mock LLM (canned replies); default judge in Docker via `JUDGE_BASE_URL` |
| `demo/synthetic_pipeline/` | five-agent OpenInference demo (orchestrator, scraper, translator, compliance, publisher) |
| `db/` | Alembic migrations (full Postgres schema) |
| `docker/clickhouse/` | ClickHouse init (`otel_spans` table) |

### Ports (docker-compose)

| Service | Port |
|---|---|
| ingest | 8001 |
| api | 8000 |
| web | 5173 |
| mock-llm | 8080 |

The bundled mock LLM is the default judge (`JUDGE_BASE_URL` defaults to
`http://mock-llm:8080/v1`), so the full stack — including blame analysis — runs
with no external API keys. To use a real judge, point `JUDGE_BASE_URL` /
`JUDGE_MODEL` / `JUDGE_API_KEY` at any OpenAI-compatible endpoint (local Ollama,
OpenRouter, …) via `.env` (gitignored). Set `TIER2_SAMPLE_PCT=100` to score every
graph's nodes, not only flagged ones.

### Milestone map

M0 skeleton · M1 `blame_engine` · M2 `otel_mapper` · M3 `ingest` · M4 `worker`
· M5 `api` · M6 demo + E2E acceptance · M7 web UI · M8 docs + licenses + CI.
