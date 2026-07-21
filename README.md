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
web/               UI placeholder (real UI in milestone M7)
```

## Quickstart

```bash
docker compose up --build    # full stack: infra + ingest + worker + api + web
./demo/run.sh                # happy-path demo run (lands in M6)
./demo/inject_fault.sh && ./demo/run.sh   # fault injection: incident appears (M6)
```

Local development (no Docker required):

```bash
uv sync                      # install the workspace
uv run pytest                # run all package tests
```

## License

- `packages/otel_mapper`: Apache-2.0
- Everything else: Business Source License 1.1 (BSL 1.1)

LICENSE files land in milestone M8.
