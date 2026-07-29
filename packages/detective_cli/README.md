# agent-detective

Blame analysis for multi-agent runs. Point it at an OpenTelemetry trace and it
tells you whether the run passed — and if not, **at which node quality broke**,
what kind of fault it was, and how confident that attribution is.

```bash
pip install agent-detective
detective analyze trace.json
```

> **🧪 Beta — the analysis is real, the verdicts are not frozen.** Everything
> documented here works and is tested. What is not yet stable is the wording of
> the answer: classifications and report shapes still change between minor
> versions. 0.2.0 reclassified a run that 0.1.0 had called `loop_detected` —
> same trace, different verdict, because the loop check had been counting the
> wrong thing. Gate CI on the **exit code** (see below), which is stable, rather
> than on an exact `report_type`, until this notice goes away.

No database, no broker, no object store, and by default no LLM. The command
runs the same two-tier pipeline the deployed service runs (the processors are
imported, not reimplemented) against in-memory implementations of its
persistence, stream and object-store seams.

## What you get

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

Three output modes: the terminal report above, `--json` for the complete typed
verdict, and `--markdown` for a findings brief you can hand to a coding agent.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | analysed, no incident |
| 1 | at least one incident |
| 2 | the analysis could not run (unreadable file, no agent spans) |

So it gates a build directly — `detective analyze trace.json` in CI fails the
job when a run ships a defect. `--exit-zero` reports without gating.

`detective doctor` is deliberately outside this table: it is a diagnostic, not
a gate, and exits 0 whatever it finds (2 only when the path cannot be opened).

## Getting a trace in: `detective capture`

The analysis reads a trace, so something has to produce one. `detective capture`
serves the single endpoint an OTLP exporter calls — `POST /v1/traces` — so an
already-instrumented agent sends its spans here with **no code change**:

```bash
detective capture --once --out run.json        # terminal 1: waits for the trace

OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8900 \
  python -m your_agent                          # terminal 2: your run
```

When the run ends, terminal 1 prints the verdict and exits 1 if there was an
incident. It binds to loopback only (pass `--host 0.0.0.0` deliberately to
receive from a container), holds the run in memory, and writes nothing unless
you ask with `--out`.

Python's stock `OTLPSpanExporter` serializes **protobuf**, which this does not
accept — that is what `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` is for. Send
protobuf anyway and the error says exactly that, rather than a bare 400.

## Before you trust a report: `detective doctor`

Bad instrumentation fails **silently**. An app that sends
`{"ok": true, "step": "collect"}` where node outputs belong still produces a
complete-looking analysis — every node scored, confidence high, and every one of
those scores describing a progress ping. What the trace did not capture at run
time, no later analysis can manufacture, so the place to catch it is up front:

```bash
detective doctor trace.json
```

```
   Capture
     ok   agent names       3 of 3 runs carry gen_ai.agent.name
     ok   edges             2 edge(s) across 3 runs
     ok   payloads          3/3 runs carry input.value and output.value
     warn payload content   3 of 3 outputs are STATUS RECORDS, not work: `resolve` →
                            {ok, step}; `collect` → {ok, step, documents}; `write` →
                            {ok, step, chars, elapsed_ms}
                            → per-node quality cannot be judged from this — a judge
                            handed {"ok": true, "step": ...} grades the progress ping,
                            not the step, and the report stays confident about work it
                            never saw
                            fix: put the step's actual product in output.value (the
                            document, the rows, the answer). Keep the status object in a
                            separate attribute if you need it

   What you can claim
     no   localization    no node carries a judgeable payload — every node reports
                          unscored
     no   cost            no run carries gen_ai.usage.cost — cost stays unknown
     no   terminal check  the deliverable's content is not visible in the trace
```

It checks AGENT spans (a span without `openinference.span.kind=AGENT` never
becomes a node — the most common silent failure), agent names, edges and
topology, the deliverable node, payload presence and payload *content*, the
`gen_ai.usage.*` / `gen_ai.request.model` attributes, and whether the
deliverable carries its artifact text or only a file reference. Every finding
states the consequence and a concrete fix.

**It also says when it cannot tell.** A router emitting
`{"action": "escalate_to_legal"}` and a progress ping are the same shape from
outside, so the doctor reports those payloads as *too short or too generic to
tell whether they carry work* and the affected claim comes back `?`, not `yes`
and not `no`. Guessing either way is the failure this command exists to catch —
in `--json` an unsettled claim is `"supported": null`.

It produces **no verdict** and **never gates**: `doctor` always exits 0,
whatever it finds. `--json` for the same diagnosis as data.

## Input

`detective analyze` reads the OTLP/HTTP **JSON** encoding of
`ExportTraceServiceRequest`: a single export object, a JSON array of them, or
JSON-lines. Any OpenInference / OpenLLMetry-instrumented agent produces this;
the analysis reads AGENT spans, so a trace containing only LLM or tool spans has
no graph to work with and the command says so rather than reporting an empty
pass.

## The two evidence channels

**Deterministic** — rules over the trace payloads: contract breaches (a carried
parameter silently rewritten), named signals (missing required sections,
artifact integrity, retry storms, duplicate side effects, injection
signatures), loop anomalies, and whether a breach actually propagated into the
shipped artifact. This channel needs nothing but the trace, which is why it is
the default.

**Judged** — a per-node quality judge, role-aware (a verifier is judged on
whether its PASS/FAIL verdict was correct, not on the artifact's quality). It
needs a model. Set `JUDGE_BASE_URL` and `JUDGE_MODEL` (any OpenAI-compatible
endpoint — hosted, Ollama, or a local server) to turn it on:

```bash
JUDGE_BASE_URL=http://localhost:11434/v1 JUDGE_MODEL=qwen2.5 \
  detective analyze trace.json
```

With no judge, nodes are reported **unscored** rather than passing, and the
report states that the judged channel did not run. A verdict that quietly
counted "not measured" as "fine" is the exact failure this tool exists to
catch.

## Honest limits

What the trace did not capture at run time, no later analysis can manufacture.
Absent evidence renders as `unverified`, never as `ok`. Registered checks that
need a rules registry (per-graph required sections, output JSON schemas) are
inert locally — the deployed service holds that registry.

## Using it as a library

```python
from detective_cli import analyze, bundles_from_exports, load_trace

run = analyze(bundles_from_exports(load_trace(Path("trace.json"))))
for graph in run.incidents:
    print(graph.blame_report["report_type"], graph.blame_report["culprit_run_ids"])
```

## License

Business Source License 1.1 — see `LICENSE`. The `otel-mapper` dependency is
Apache-2.0.
