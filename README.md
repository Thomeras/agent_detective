# Agent Detective

[![CI](https://github.com/Thomeras/agent_detective/actions/workflows/ci.yml/badge.svg)](https://github.com/Thomeras/agent_detective/actions/workflows/ci.yml)
[![License: BSL 1.1](https://img.shields.io/badge/license-BSL%201.1-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**PagerDuty for multi-agent systems.** OTEL-native observability with **blame
analysis**: ingest standard OpenTelemetry traces from your agents, rebuild the
execution graph, score every node, and name the first node where quality
broke — the culprit, the propagation path, and the downstream cost.

No custom SDK, no proprietary protocol: point any OpenInference / OpenLLMetry
instrumented agent at the endpoint and it just works.

```bash
pip install agent-detective
detective analyze trace.json
```

```
Agent Detective — trace.json
1 graph(s) · 5 agent run(s) · judged channel: OFF — not configured

── graph 3f2a91c8  [content-pipeline]
   FAILED  ·  cut_point  ·  confidence 62%
   Quality demonstrably broke at a localized origin.

   Origin — where quality broke
     translator

   Defects
     ● Contract breach — translator
       A carried input/output parameter was silently rewritten at translator.
       observation 100% · attribution 92% · channel deterministic
       supporting: contract_breach at translator (rule: contract_param_rewrite)
```

## Where to start

| You want to… | Read |
|---|---|
| Analyze one run right now, zero infrastructure | [Quickstart](#quickstart) below |
| The guided tour — instrumenting, analyzing, CI, machine output | **[docs/usage.md](docs/usage.md)** |
| Connect your own agents (attributes, adapters, SDK) | [docs/instrumentation.md](docs/instrumentation.md) |
| Understand the system design | [docs/architecture.md](docs/architecture.md) |
| What it can and cannot claim today | [docs/capabilities.md](docs/capabilities.md), [docs/trace-requirements.md](docs/trace-requirements.md) |
| The pip distribution's own manual | [packages/detective_cli/README.md](packages/detective_cli/README.md) |
| Benchmark against Who&When failure attribution | [benchmarks/whoandwhen/README.md](benchmarks/whoandwhen/README.md) |

## Quickstart

Local mode — one trace file, no infrastructure at all:

```bash
pip install agent-detective
detective capture --once --out run.json # receive a trace straight from your agent
detective analyze run.json              # the verdict, in the terminal
detective analyze run.json --markdown   # a findings brief for a coding agent
detective doctor run.json               # is the trace even worth trusting?
```

`capture` serves `POST /v1/traces`, the one endpoint an OTLP exporter calls, so
an already-instrumented agent points at it with no code change:

```bash
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8900
```

Not instrumented yet? `detective-sdk` (pure stdlib, zero dependencies) emits the
same standard spans from a context manager per step — nothing to adopt, nothing
to lock into:

```python
from detective_sdk import run

with run("intel", task=user_request) as r:
    with r.step("resolve") as s:
        s.output = company                 # the work, not {"ok": true}
    with r.step("write") as s:
        s.output = dossier_markdown
        s.cost(usd=0.03, tokens_in=8_000, tokens_out=900, model="gpt-4o")
```

It is off unless `AGENT_DETECTIVE_ENDPOINT` or `AGENT_DETECTIVE_TRACE_FILE` is
set, so instrumented code ships to production untouched. Fan-out, joins and
retry loops are covered too — see [docs/usage.md](docs/usage.md#12-nothing-instrumented-yet--detective-sdk).

Local mode runs the **same** tier1/tier2 processors the deployed worker runs
(they are imported, not reimplemented) against in-memory implementations of the
persistence, stream and object-store seams — so a local verdict is comparable to
a deployed one. It exits 1 on an incident, which makes it a CI gate as-is. No
database, no broker, no object store, and by default no LLM: the deterministic
evidence channel needs nothing but the trace, and the report states plainly that
the judged channel was off rather than passing one channel off as both. Set
`JUDGE_BASE_URL` / `JUDGE_MODEL` (any OpenAI-compatible endpoint) to turn
per-node judging on.

## What the blame report tells you

For each incident the engine names **where quality broke** (the *origin* — the
first node whose score dropped from a healthy predecessor, drilled into the
worst member when that node is inside a retry loop) and separates it from **where
it surfaced** (the *manifestation* — the terminal output). It flags **rubber-
stamping verifiers** (a `qa`/`eval` node that passed bad work — `verification_gap`),
reports **honest, capped confidence** (a fallback verdict is never sold as
certainty), and includes a per-node **candidacy trace** explaining why each node
was or wasn't blamed. Report types (derived from the typed defects, never
templated): `cut_point`, `multi_culprit`, `verification_gap`, `loop_detected`,
`root_cause_external`, `composition_failure`, `degraded_recovered`,
`shipped_with_latent_defect`, `terminal_defect_unlocalized`, `unclassified`.
Every graph view exports a Markdown **findings brief** (`Export .md`) you can
hand to a coding agent to drive the fix.

Two evidence channels feed the verdict:

- **Deterministic** — reproducible rules over the trace: contract breaches,
  named signals (missing sections, artifact integrity, retry storms), loop
  anomalies, breach propagation into the shipped artifact. Needs nothing but
  the trace; always on.
- **Judged** — a per-node quality judge behind any OpenAI-compatible endpoint
  (`JUDGE_BASE_URL` / `JUDGE_MODEL` / `JUDGE_API_KEY`), **role-aware**:
  producer nodes are judged relative to their input, verifier nodes on the
  correctness of their verdict. Opt-in; with no judge, nodes report *unscored*,
  never silently "fine".

## The full stack

Continuous ingest, incident inbox, graph UI, agent leaderboard, cross-run
history:

```bash
docker compose up --build    # full stack: infra + ingest + worker + api + web
./demo/run.sh                # happy-path demo run: graph appears, no incident
./demo/inject_fault.sh && ./demo/run.sh   # fault injection: a cut_point incident appears
```

Web UI at `http://localhost:5173`, read API at `:8000`, ingest at `:8001`. The
bundled mock LLM serves as the judge, so the stack runs with **no external API
keys**.

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
  detective_cli/   the `agent-detective` pip distribution: local mode + CLI
  detective_sdk/   zero-dependency instrumentation helpers for integrators
  detective_ci/    deterministic golden replay + pytest plugin (CI gate)
services/
  ingest/          OTLP/HTTP trace ingest + graph finalizer
  worker/          tier1/tier2 pipeline over Redis Streams, judge client, alerter
  api/             read API for graphs, incidents, blame reports, agent stats
db/                Alembic migrations (full Postgres schema)
docker/clickhouse/ ClickHouse init (otel_spans table)
web/               React + Vite + cytoscape UI: incident inbox, graph list,
                   graph view (loop-aware), agent leaderboard, findings export
```

## Development

```bash
uv sync --all-packages --all-groups   # install the workspace
./scripts/test.sh                     # run every unit suite (mirrors CI)
```

Each suite runs from its own package directory (`scripts/test.sh` handles
this); the end-to-end acceptance test lives in `tests/e2e` and needs a running
stack (`uv run pytest tests/e2e`).

Building the pip distribution (CI does this on every push, then installs it
into a clean environment and analyses the demo traces with it):

```bash
for p in blame-engine otel-mapper agent-detective-worker agent-detective; do
  uv build --package "$p"
done
```

Distribution names are namespaced where the bare name is not ours: the worker
ships as `agent-detective-worker` because `worker` on PyPI belongs to an
unrelated project, and pip merges the index with any local `--find-links`.

## Honest limits

What the trace did not capture at run time, no later analysis can manufacture
— reprocessing replays interpretation, it never adds evidence. The concrete
claim → required-capture mapping lives in `docs/trace-requirements.md`; absent
evidence renders as `unverified`, never as `ok`.

The live-validated corpus is still one harness, one linear topology, one
injected fault. Non-linear behaviour (fan-out branch-vs-join blame,
asymmetric propagation, independent multi-culprit branches, retry loops) is
locked by hand-authored engine fixtures, but the graph-first thesis has not
yet met a real fan-out/A2A trace. Judge determinism claims are scoped to the
recorded stack (model, endpoint, temperature, prompt hashes travel with every
cassette); a different stack may float where this one did not.

## License

- `packages/otel_mapper`: Apache-2.0 (see `packages/otel_mapper/LICENSE`)
- Everything else: Business Source License 1.1 (BSL 1.1) (see `LICENSE`)
