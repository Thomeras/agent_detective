# Foreign-trace corpus

Every other suite in this repository feeds the engine spans this project wrote
itself. That is enough to catch a blame-engine regression and useless for
catching a mapper that disagrees with the real OpenTelemetry SDK — our exporter
and our mapper can be wrong in the same direction forever and every test stays
green.

These traces come from [agent_topo_db](https://github.com/Thomeras/agent_topo_db):
22 multi-agent topologies with **no telemetry of their own** and no knowledge
that this analysis exists. They are instrumented from outside, by stock
`opentelemetry-sdk`, and the faults are injected from outside too — so every
entry carries ground truth about which node broke.

```
corpus/
  corpus/otel_bridge.py      stock OTel SDK -> OTLP/HTTP JSON on disk
  corpus/topolab_adapter.py  agent spans + edges, added without touching agent_topo_db
  corpus/inject.py           the fault library
  corpus/record.py           the recorder (developer step, costs money)
  traces/                    the corpus: trace + .label.json ground truth
  tests/                     hermetic replay — no model, no network
```

## Two facts the design turns on

**The context dict is the graph.** `topolab.Agent.run(task, context)` takes a
mapping whose keys are the names of the agents whose output is being passed in.
The topology author wrote the execution graph down for entirely unrelated
reasons, so all 22 topologies are traceable with no edits to any of them.

**Auto-instrumentation alone reconstructs to nothing.**
`openinference-instrumentation-openai` emits `LLM` spans, and the mapper opens a
run only on `openinference.span.kind=AGENT`. A purely auto-instrumented run of
these topologies produces a graph with zero nodes. The agent layer has to be
stated by an adapter — which is the situation of every real framework
integration, and the reason this corpus is worth more than another synthetic
fixture.

## Recording (developer step)

Needs agent_topo_db checked out and a live OpenRouter key. Costs real money;
CI never does this.

```bash
uv sync --all-packages --all-extras
export OPENROUTER_API_KEY=...

# negative control and its faulted twin, from byte-identical topology code
uv run python -m corpus.record --topo-db ~/Projekty/agent_topo_db --topology 21_fan_in_join
uv run python -m corpus.record --topo-db ~/Projekty/agent_topo_db --topology 21_fan_in_join \
    --fault drop_numbers --target metrics_analyst
```

A fault that never fires refuses to write an entry, and a crashed run deletes
its own half-written trace: a mislabelled cell is worse than a missing one.

## Replay (what CI runs)

```bash
cd corpus && uv run --package agent-detective-corpus pytest tests
```

Assertions are **deterministic-channel only**. Asserting a judged verdict would
put a model in CI, which costs money and stops being reproducible. The judged
observations below are recorded as findings, not asserted as behaviour.

## What it found on the first two cells

Written down because it is the point of the exercise, not because it is
finished. Three of these are open.

**1. The graph reconstructs correctly from foreign spans.** Topology 21's
fan-in came back exactly right — two parallel branches, the join reading both,
sources on the root — derived from the context keys, through the stock SDK,
across `ThreadPoolExecutor` boundaries. The mapper's `attempt`/`attempt_of`
handling (added in 0.2.0) also works on spans it did not produce: the nested
loops of topology 13 came back with correct per-agent attempt ordinals.

**2. `empty_output` fires on a foreign trace and localises.** `performance_join`
spent 2000 output tokens and returned nothing; the deterministic channel named
it as origin with a 100%-certainty signal. Note the cause: `moonshotai/kimi-k2.6`
spends its whole `max_tokens` budget on reasoning and returns empty content —
reproduced at 1200 and again at 2000 tokens, on two unrelated topologies. Kept
as its own labelled cell.

**3. OPEN — the judge did not notice the injected defect.** Stripping *every
number* out of `metrics_analyst`'s output changed nothing: clean and faulted
both came back `composition_failure · 40%`, and **every node scored 0.93 in both
runs**, including the one whose metrics analysis no longer contained a single
figure. The judge is grading fluency, not substance. This is the calibration
question the corpus exists to make measurable.

**4. OPEN — a deterministic-only localisation reports 0% confidence.** The
`empty_output` cell names an origin, cites a finding at `certainty 100%`, and
prints `confidence 0% · observation 0% · attribution 0%`. Confidence appears to
be derived from judged score movement, which does not exist when the node is
unscored — so the honest-confidence claim reads as "no idea" precisely where the
evidence is hardest.

**5. OPEN — the structural root was named ORIGIN.** In the clean cell the
culprit is `21_fan_in_join`, the payload-less run wrapper, which `cutpoint.py`
elsewhere describes as a node that "can neither be a culprit nor hide one".

## What is not here yet

The injection matrix (topology × fault × site) this is the foundation for. Six
faults are implemented; two cells are recorded. Metamorphic invariance,
adversarial/degraded traces and the reliability diagram all need the grid first.
