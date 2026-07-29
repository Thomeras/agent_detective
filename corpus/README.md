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

`uv run python -m corpus.scoreboard --repeats 5` (needs a judge; output committed
as `scoreboard.json`). 18 cells over 6 topologies, every cell run
5× because the judged channel is not deterministic:

| | | |
|---|---|---|
| **false positive rate** | **0.24** | 95% CI over 5 control cells [0.118, 0.769] |
| **discrimination** | **0.582** | CI over 11 faulted cells [0.354, 0.848] |
| **attribution accuracy** | **0.431** | CI over 13 cells with a known origin [0.232, 0.709] |

Three numbers because they fail independently. Detection says an incident was
reported; discrimination says the report differs from the same topology's clean
baseline (a verdict that fires on everything scores well on detection and is
worth nothing); attribution says the named node is the one that actually broke.
Attribution is the product's claim, and it is the weakest of the three.

The intervals are over CELLS, not runs. Six topologies say little about the 22
that exist, and no number of repeats fixes that — a tight interval off 50 runs of
5 controls would be arithmetic dressed as evidence.

## What it found

**0. Two attempts at attribution; neither moved it. Reported as measured.**
The chain is now understood end to end: on the boundary-adapter cell the judge
scores `partner_feed_adapter` **0.9** despite output that contradicts its own
input arithmetically, `performance_join` gets 0.55, and the only measured drop
is therefore at the join — the cut-point logic is doing exactly its job on the
data it is given. The defect is upstream of blame, in per-node scoring.

*Attempt one — tell the judge to re-derive transformations.* Added an
instruction to take the numbers out of the input, apply the stated rule and
compare figure by figure. Measured back-to-back in one session: attribution
0.385 → 0.385, discrimination 0.485 → 0.485. The adapter still scored 0.9.
Reverted.

*Attempt two — stop treating "no complaints" as a 1.0.* `evaluate_heuristics`
only ever SUBTRACTS, so on an ordinary run it fired nothing and returned a full
1.0, which the composite averaged in as positive evidence worth ~27% of every
score. It now returns None when no check fires. This is right on its own terms
and provably so: a node the judge caps at 0.45 for `factual_error` used to come
out at 0.60 — above the acceptance bar, so the criticism the judge had written
changed nothing — and now lands at 0.45. Kept, with a unit test pinning it.

But the corpus cannot show that benefit, because in these cells the judge never
raises the flag at all: it scores the wrong conversion 0.9. Attribution moved
0.385 → 0.41, discrimination 0.485 → 0.515, FPR 0.133 → 0.2 — every one of them
inside the noise this corpus has already measured. So: one fix kept on evidence
that is a unit test rather than a rate, one reverted, and the blocker unchanged
and now precisely located.

**1. Attribution is the weak link, and it fails in one direction.** 8 of 13
cells with a known origin do not name it. The pattern is consistent: the report
localises where the defect becomes VISIBLE, not where it was MADE. Both
`22_join_external_adapter` cells are the clean case of it — the truth is
`partner_feed_adapter`, which mangled the numbers, and the report says
`performance_join`, the node that merged them. Naming the culprit rather than
the symptom is the product's whole claim, so this is the number to move.

**2. The corpus caught a fault nobody injected, and the label was wrong — mine.**
`22_join_external_adapter__clean` was recorded as a control and consistently
reported an incident. Checking the arithmetic by hand against the source feed:
`partner_feed_adapter` ran the PERCENTAGES through the exchange rate — 340 basis
points is 3.40% and it emitted 1.38 (= 3.40/2.46), 180 bp is 1.80% and it emitted
0.73. Revenue is wrong too (184300 EUR x 24.6 = 4533780, it emitted 4538580).
A real unit cross-contamination at the boundary node, produced by the model. It
is now labelled as the true positive it is, with the verified origin as ground
truth — and the engine still blames the joiner for it.

**3. A judged verdict does not reproduce across sessions.**
`05_diamond__clean` returned no incident on 5 of 5 runs in one scoreboard
invocation, and `composition_failure` on 6 of 6 an hour later — byte-identical
trace, unchanged code, unchanged prompt. Repeats inside one invocation cannot see
that, so the intervals above understate it. This is the strongest argument in the
corpus for the deterministic channel carrying the weight the judged one cannot.
Two further cells (`20_fixed_review_loop__clean`,
`21_fan_in_join__drop_numbers_at_metrics_analyst`) are unstable WITHIN a single
invocation as well.

**4. Injection site does not rescue detection.** Same fault (`drop_numbers`),
same 4-step pipeline, three sites — head (`log_ingestor`), middle (`enricher`
via truncate), tail (`report_writer`). None of the three was noticed. The
judged channel reads fluency, and stripping figures leaves prose fluent
wherever it happens.

**5. CORRECTED — the 1.00 false positive rate reported earlier was this
harness.** The first recorder wrote `"agent_topo_db topology 05_diamond"` into
the run root's `input.value`. That field is the ORIGINAL REQUEST and the only
thing the terminal judge can check the deliverable against, so the judge was
handed a string that is not a request and correctly reported that the output did
not answer it. With the real `TASK`, FPR is 0.28.

The diagnosis built on that number ("the judge grades against an ideal rather
than against the request") was then tested and rejected. A rewritten terminal
prompt — delivery not excellence, "bad" must name a specific absent or invented
element, anchored score scale — measured against the same corpus:

| prompt | FPR | discrimination |
|---|---|---|
| shipped | unchanged | 0.67 – 0.83 |
| rewritten | unchanged | 0.33 |

It moved the false positive rate not at all and halved detection. Reverted. That
is the return on building this: a plausible fix could be refuted in an afternoon
instead of shipped.

**6. The deterministic channel and the mapper are the solid parts.**
`empty_output` localises at 95% on foreign spans, every time. Every graph
reconstructed correctly from stock-SDK output — topology 21's fan-in across
`ThreadPoolExecutor` boundaries, 13's nested loops with correct per-agent attempt
ordinals, 12's supervisor fan-out, 20's review loop.

**7. FIXED — a deterministic-only localisation reported 0% confidence.** Two
predicates for "this node has a hard deterministic defect" had drifted apart:
`cutpoint._deterministic_defect` counts any fail-severity signal and decides
whether the node is localised at all, while `blame._has_deterministic_defect`
read only contract violations and a closed set of three flag names, and drives
the confidence. Now 95/95, with the deterministic attribution headline earned by
an explicit `originates` marker rather than granted to every fail signal.

## What is not here yet

18 cells over 6 of the 22 topologies. No metamorphic invariance (span order,
batch splits, node renames), no adversarial traces (orphaned spans, duplicate
ids, clock skew), no reliability diagram — and the third of those needs more
cells with known origins before binning means anything.

But the queue should start with finding 1. Attribution accuracy 0.385 is the
number that contradicts the product's own sentence, and every cell added before
it moves measures the same miss again.
