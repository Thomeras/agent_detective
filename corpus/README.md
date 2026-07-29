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

## The scoreboard

`uv run python -m corpus.scoreboard` (needs a judge; output committed as
`scoreboard.json`). Two numbers, 11 cells across 4 topologies:

| | |
|---|---|
| **false positive rate** | **1.00** — 4 of 4 clean controls reported an incident |
| **discrimination** | **0.50** — 3 of 6 faulted cells produced a verdict differing from their own topology's clean baseline |

Discrimination is measured against the PAIRED baseline, not against an absolute
expectation. A verdict that fires on everything scores well on "did it report an
incident" and is worth nothing; only the pairing makes that visible.

Verdict distribution: `composition_failure` 7, `cut_point` 3, `multi_culprit` 1.

## What it found

**1. Every clean control fails. FPR is 1.00.** This is the number that decides
whether anyone can gate a build on this tool, and right now it says nobody can.
The cause is the terminal judge, not the engine: on an untouched run of
`02_pipeline` it returns `bad` at 0.4 with

> "lacks context regarding the overall performance of the system and does not
> provide a comprehensive analysis of the pipeline's effectiveness, which is
> essential for a complete report"

That is not a defect report, it is a wish for a better document. The judge is
grading against an ideal rather than against the request that was actually made.
`composition_failure` then fires by construction — every node healthy, terminal
bad — which is why it is 7 of 11 verdicts.

**2. Half the injected faults are invisible.** Stripping *every number* out of a
metrics analysis, appending a fabricated audit claim, and rewriting a currency
unit at a boundary adapter all produced verdicts identical to their own clean
baseline, down to the confidence. Per-node scores clustered at 0.93 regardless.
The judge is grading fluency; none of these three faults damages fluency.

**3. The two things that DO work are the deterministic channel and the graph.**
`empty_output` localised at 95% on a foreign trace. `truncate` and `drop_numbers`
at a pipeline head both localised as `cut_point`. And every graph reconstructed
correctly from stock-SDK spans — topology 21's fan-in across `ThreadPoolExecutor`
boundaries, topology 13's nested loops with correct per-agent attempt ordinals.
The mapper is in better shape than the judged channel.

**4. FIXED — a deterministic-only localisation reported 0% confidence.** The
`empty_output` cell named an origin, cited a finding at `certainty 100%`, and
printed `confidence 0%`. Two predicates for "this node has a hard deterministic
defect" had drifted apart: `cutpoint._deterministic_defect` counts any
fail-severity signal and decides whether the node is localised at all, while
`blame._has_deterministic_defect` read only contract violations and a closed set
of three flag names, and drives the confidence. Now 95/95, with the deterministic
attribution headline earned by an explicit `originates` marker on the signal —
blanket-granting it would over-claim for a signal like an injection signature,
which says the output is bad and nothing about where it came from.

**5. NOT A BUG — the run wrapper named as composition_failure suspect.** Called
this one wrong on first reading. An orchestrator entry node and an SDK wrapper
span are the SAME SHAPE in a trace — both are sources with no output of their
own — so the engine cannot tell them apart, and
`test_composition_failure_all_healthy_bad_terminal` deliberately pins blaming the
entry node. Left alone.

## What is not here yet

11 cells over 4 of the 22 topologies, and no coverage yet of retry loops,
supervisor hierarchies or wide fan-out. Injection SITE is not varied — every
fault lands on a node picked by hand rather than early/middle/late and
branch-vs-merge. Metamorphic invariance (span order, batch splits, node renames)
and adversarial traces (orphaned spans, duplicate ids, clock skew) are not
started; the reliability diagram needs enough cells to bin.

None of that is the bottleneck. Finding 1 is: while the terminal judge rejects
clean work, every additional cell measures the same miscalibration again.
