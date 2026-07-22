# Agent Detective

PagerDuty for multi-agent systems. OTEL-native observability with **blame
analysis**: ingest standard OpenTelemetry traces from your agents, build the
execution graph, score every node, and find the first node where quality
broke — the culprit, the propagation path, and the downstream cost.

No custom SDK, no proprietary protocol: point any OpenInference/OpenLLMetry
instrumented agent at the ingest endpoint and it just works.

## Architecture

```
                 OTLP/HTTP
 your agents ───────────────▶ ingest ──▶ ClickHouse (raw spans)
                                 │
                                 ├──▶ Postgres (graphs, runs, edges, payloads*)
                                 │
                                 └──▶ Redis Streams ──▶ worker tier1
                                 (ad.graphs.completed)   (cheap checks + terminal judge)
                                                            │ flagged / sampled
                                                            ▼
                                                       worker tier2
                                                    (per-node scoring + judge)
                                                            │
                                                            ▼
                                                      blame engine
                                                    (pure networkx package)
                                                            │
                                                            ▼
                                        incidents + blame reports (Postgres)
                                                            │
                                                            ▼
                                              read API (FastAPI) ──▶ web UI (React)

 * payload overflow lives in MinIO (bucket: agent-detective-payloads)
```

## Repository layout

```
packages/
  blame_engine/    pure, I/O-free blame analysis (networkx only)
  otel_mapper/     OTLP/HTTP JSON span -> AgentRun/Edge mapping (Apache-2.0)
services/
  ingest/          OTLP/HTTP trace ingest + graph finalizer
  worker/          tier1/tier2 pipeline over Redis Streams, judge client, alerter
  api/             read API for graphs, incidents, blame reports, agent stats
db/                Alembic migrations (full Postgres schema)
docker/clickhouse/ ClickHouse init (otel_spans table)
web/               React + Vite + cytoscape UI: incident inbox, graph list,
                   graph view (loop-aware), agent leaderboard, findings export
```

## What the blame report tells you

For each incident the engine names **where quality broke** (the *origin* — the
first node whose score dropped from a healthy predecessor, drilled into the
worst member when that node is inside a retry loop) and separates it from **where
it surfaced** (the *manifestation* — the terminal output). It flags **rubber-
stamping verifiers** (a `qa`/`eval` node that passed bad work — `verification_gap`),
reports **honest, capped confidence** (a fallback verdict is never sold as
certainty), and includes a per-node **candidacy trace** explaining why each node
was or wasn't blamed. Report types: `cut_point`, `multi_culprit`,
`verification_gap`, `loop_detected`, `root_cause_external`, `composition_failure`,
`unclassified`. Every graph view exports a Markdown **findings brief** (`Export
.md`) you can hand to a coding agent to drive the fix.

The per-node quality judge is any OpenAI-compatible endpoint
(`JUDGE_BASE_URL` / `JUDGE_MODEL` / `JUDGE_API_KEY`) — the bundled mock LLM by
default, or a local/hosted model — and is **role-aware**: producer nodes are
judged relative to their input, verifier nodes on the correctness of their
verdict.

## Quickstart

```bash
docker compose up --build    # full stack: infra + ingest + worker + api + web
./demo/run.sh                # happy-path demo run: graph appears, no incident
./demo/inject_fault.sh && ./demo/run.sh   # fault injection: a cut_point incident appears
```

Local development (no Docker required):

```bash
uv sync --all-packages --all-groups   # install the workspace
./scripts/test.sh                     # run every unit suite (mirrors CI)
```

Each suite runs from its own package directory (`scripts/test.sh` handles
this); the end-to-end acceptance test lives in `tests/e2e` and needs a running
stack (`uv run pytest tests/e2e`).

## License

- `packages/otel_mapper`: Apache-2.0 (see `packages/otel_mapper/LICENSE`)
- Everything else: Business Source License 1.1 (BSL 1.1) (see `LICENSE`)

Docs: `docs/architecture.md` (system design), `docs/instrumentation.md`
(connecting your own agents).
