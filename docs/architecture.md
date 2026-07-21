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
  every node (`worker/scoring.py`: schema / judge / heuristics components,
  weight-renormalized, judge run per-node relative to that node's input), runs
  the blame engine, enriches the report with post-blame fact propagation, and
  persists incidents and blame reports.

Because tier1's terminal judge sees the *full* terminal output (not a summary),
it does not throw away the very facts that distinguish a hallucination — one of
the design defects the revised spec fixes.

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

3. **Cut-point candidates** (`cutpoint.py`). Over the condensation DAG in
   topological order, a node is a candidate when it is below `threshold` **or**
   drops from its best known predecessor by at least `gap_threshold`, provided
   the drop is at least `min_drop` (distinguishing an *origin* of degradation
   from inherited low quality). **Shadowing** discards any candidate that has
   another candidate among its ancestors; the survivors are independent origins.
   Unknown-scored nodes are never culprits.

4. **Confidence** (`confidence.py`) blends the drop magnitude, severity, and
   predecessor quality, then applies penalties (multi-member SCC culprit,
   multi-culprit) and caps confidence when an unknown predecessor sits anywhere
   upstream.

5. **Classification** (`blame.py`, first match wins) yields one of six report
   types:

   | report_type | when |
   |---|---|
   | `unclassified` | all scores unknown (`no_scores`), or nothing else matched |
   | `root_cause_external` | the single unshadowed candidate is a condensation source with `input_flawed = True` |
   | `loop_detected` | an anomalous loop is the culprit (or there are no candidates); culprits are its members |
   | `cut_point` | exactly one unshadowed candidate |
   | `multi_culprit` | more than one unshadowed candidate (parallel branches) |
   | `composition_failure` | no candidate, terminal verdict bad, all scored nodes healthy, no significant drop, no unknowns — culprit is the source/orchestrator |

The report also carries the **propagation path** (shortest path in the
condensation DAG from culprit to the terminal super-node, SCCs expanded to
members by end-time) and the **downstream cost** (the culprits' own cost plus
all their descendants in the original graph, deduplicated).

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

## Component map

| Path | Component |
|---|---|
| `packages/blame_engine/` | pure, I/O-free blame analysis (networkx only) — **BSL 1.1** |
| `packages/otel_mapper/` | OTLP/HTTP JSON span → run/edge mapping — **Apache-2.0** |
| `services/ingest/` | OTLP/HTTP trace ingest, ClickHouse/Postgres/MinIO writes, finalizer |
| `services/worker/` | tier1/tier2 pipeline over Redis Streams, judge client, alerter, DLQ reaper |
| `services/api/` | FastAPI read API: graphs, incidents, blame reports, agent leaderboard, manual analyze |
| `web/` | React + Vite + TypeScript + cytoscape.js UI (dark-mode-first): incident inbox, graph view, agent leaderboard |
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
with no external API keys.

### Milestone map

M0 skeleton · M1 `blame_engine` · M2 `otel_mapper` · M3 `ingest` · M4 `worker`
· M5 `api` · M6 demo + E2E acceptance · M7 web UI · M8 docs + licenses + CI.
