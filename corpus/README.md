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
uv run --all-extras python -m corpus.record --topo-db ~/Projekty/agent_topo_db \
    --topology 21_fan_in_join
uv run --all-extras python -m corpus.record --topo-db ~/Projekty/agent_topo_db \
    --topology 21_fan_in_join --fault drop_numbers --target metrics_analyst
```

A fault that never fires refuses to write an entry, a crashed run deletes its own
half-written trace, and a topology with no `TASK` constant is refused outright —
a mislabelled cell is worse than a missing one, and finding 1 below is what that
rule was written from.

## Replay (what CI runs)

```bash
cd corpus && uv run --package agent-detective-corpus pytest tests
```

Assertions are **deterministic-channel only**. Asserting a judged verdict would
put a model in CI, which costs money and stops being reproducible. The judged
observations below are recorded as findings, not asserted as behaviour.

## The scoreboard

`uv run python -m corpus.scoreboard` (needs a judge; output committed as
`scoreboard.json`). 12 cells over 4 topologies, measured repeatedly because the
judged channel is not deterministic:

| | |
|---|---|
| **false positive rate** | **0.25** — 1 of 4 clean controls reported an incident; identical across every run |
| **discrimination** | **0.67 – 0.83** on the 6 judged-only faults; **0.75 (6/8)** with the two deterministic `empty_answer` cells included |
| **verdict stability** | **9 of 10** judged cells returned the same report type every run; the two deterministic cells never move |

Discrimination is measured against the PAIRED baseline, not against an absolute
expectation. A verdict that fires on everything scores well on "did it report an
incident" and is worth nothing; only the pairing makes that visible.

## What it found

**1. CORRECTED — the 1.00 false positive rate was this harness, not the judge.**
The first version of the recorder wrote `"agent_topo_db topology 05_diamond"`
into the run root's `input.value`. That field is the ORIGINAL REQUEST, and the
only thing the terminal judge can check the deliverable against — so the judge
was handed a string that is not a request and correctly reported that the output
did not answer it. Every clean control failed for that reason. With the real
`TASK` read off the topology module, FPR is 0.25.

The diagnosis this replaces ("the judge grades against an ideal rather than
against the request") was tested and rejected. A rewritten terminal prompt —
delivery not excellence, "bad" requires naming a specific absent or invented
element, an anchored score scale — was measured against the same corpus:

| prompt | FPR | discrimination |
|---|---|---|
| shipped | 0.25 | 0.67 – 0.83 |
| rewritten | 0.25 | 0.33 |

It moved the false positive rate not at all and halved detection. Reverted. The
lesson is about method, not about prompts: the corpus existed, so a plausible
diagnosis could be checked instead of shipped.

**2. The judged channel is not deterministic.** Same trace, same prompt, three
runs: discrimination came back 0.83, 0.67, 0.83, and
`21_fan_in_join__drop_numbers_at_metrics_analyst` returned
`composition_failure` twice and clean once. 9 of 10 cells were stable. Any single
measurement of these numbers — including one used to justify a change — is worth
less than it looks.

**3. Two faults stay invisible.** Stripping every number out of a metrics
analysis, and truncating an enricher mid-sentence, both produced verdicts equal
to their clean baseline. Neither damages fluency, which is what the judged
channel reads.

**4. The deterministic channel and the mapper are the solid parts.**
`empty_output` localised at 95% on foreign spans. Every graph reconstructed
correctly from stock-SDK output — topology 21's fan-in across
`ThreadPoolExecutor` boundaries, topology 13's nested loops with correct
per-agent attempt ordinals.

**5. FIXED — a deterministic-only localisation reported 0% confidence.** The
`empty_output` cell named an origin, cited a finding at `certainty 100%`, and
printed `confidence 0%`. Two predicates for "this node has a hard deterministic
defect" had drifted apart: `cutpoint._deterministic_defect` counts any
fail-severity signal and decides whether the node is localised at all, while
`blame._has_deterministic_defect` read only contract violations and a closed set
of three flag names, and drives the confidence. Now 95/95, with the deterministic
attribution headline earned by an explicit `originates` marker — blanket-granting
it would over-claim for a signal like an injection signature, which says the
output is bad and nothing about where it came from.

**6. NOT A BUG — the run wrapper named as composition_failure suspect.** An
orchestrator entry node and an SDK wrapper span are the same shape in a trace, so
the engine cannot tell them apart, and
`test_composition_failure_all_healthy_bad_terminal` deliberately pins blaming the
entry node. Left alone.

## What is not here yet

11 cells over 4 of the 22 topologies, and no coverage yet of retry loops,
supervisor hierarchies or wide fan-out. Injection SITE is not varied — every
fault lands on a node picked by hand rather than early/middle/late and
branch-vs-merge. Metamorphic invariance (span order, batch splits, node renames)
and adversarial traces (orphaned spans, duplicate ids, clock skew) are not
started; the reliability diagram needs enough cells to bin.

The one number that should come first is repeat count. Finding 2 says a single
scoreboard run is not a measurement, and every cell added now makes the variance
cheaper to hide rather than easier to see.
