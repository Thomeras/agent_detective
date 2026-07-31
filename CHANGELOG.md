# Changelog

Notable changes per release. Versions follow [semver](https://semver.org),
with the beta caveat stated in the README: while the project is `0.x`, a MINOR
bump may change what a verdict is *called* on an unchanged trace. The exit-code
contract (`0` analysed and clean, `1` incident, `2` analysis could not run) is
stable and is the thing to gate CI on.

Distributions are versioned independently; a release lists the ones that moved.

## [0.4.1] — 2026-07-31

`blame-engine` (0.4.0 → 0.4.1). `agent-detective` and
`agent-detective-worker` already require `blame-engine>=0.4.0`, so the fix
reaches them without a bump of their own.

**No verdict is renamed.** Both entries make a report say *less* than it did,
in the two places where it was saying more than it had measured: one number
falls, one silently dropped node reappears as evidence. `report_type`, the
exit-code contract and the schema are all unchanged, and the foreign-corpus
goldens are byte-identical to 0.4.0.

**Upgrade note.** `verification_gaps` gains entries on unchanged traces —
verifiers that were being dropped silently now appear. If you alert on gap
counts, expect a step up; it is the second fix below surfacing what was
already there, not new faults.

### Fixed

- **An LLM's flag no longer counts as deterministic proof.**
  `observation_confidence` — "how sure are we this output is defective" —
  reached `DETERMINISTIC_ATTRIBUTION` (0.95) for a node whose
  `contract_violations` and `deterministic_signals` were both empty. The
  constant means *origination observed on both sides of the fault*; what
  actually triggered it was `missing_required_content`, a string the per-node
  judge emits, recorded a few lines away in the same report as a judged
  Finding worth `certainty: 0.7`. One flag was worth 0.7 as evidence and 0.95
  as proof.

  This was the surviving half of a drift 0.3.0 reported as closed. Two
  functions answered the same question — `blame._has_deterministic_defect` and
  `cutpoint._deterministic_defect` — and had to agree by discipline. The first
  divergence (a closed set of three flag names here, any fail-severity signal
  there) made a node localised by a signal outside that set publish observation
  0% while citing a 100%-certainty finding; that fix added the fail-severity
  branch and left the judge-flag branch untouched. They are now **one
  function**, so a third drift cannot be written.

  A judged flag is judged evidence: it lands in the severity/degradation
  formula, which already reads the score it capped. Affected reports lose the
  0.95 and report what the judged channel measured — for a node scoring 0.55
  after a 0.35 drop, `0.49`.

- **A verifier that let bad work through no longer vanishes on an unchecked
  string.** `judge_verifier.md` forces exactly one of
  `issued_pass` / `issued_fail` out of every call, and nothing confronts the
  answer with the payload — so the flag is a *claim* about what the verifier
  did, not an observation of it. The engine read it as ground truth to pick
  which corroboration branch ran, and with a bad terminal an `issued_fail`
  therefore dropped the node silently: no gap, no note, nothing in evidence.

  Found on a live run where a verifier stamped `"overeno": true` over
  `sidli_tam: null` and `najem: null`, drew the harshest per-node judge score
  in the graph (0.0), and still produced `verification_gaps: []` — because the
  same judge call also emitted `issued_fail` while its own reasoning read
  *"incorrectly **passed** the work"*.

  That triple is self-contradictory and detectable without reading a word of
  prose: a FAIL on work the terminal confirms is bad is the *right* verdict, so
  a sub-threshold score ("this verdict was wrong") and the `issued_fail` flag
  cannot both hold. The engine cannot tell which of the two judge outputs
  failed, so it now reports the conflict instead of resolving it by reporting
  neither.

### Changed

- `verification_gaps` entries carry a new `basis`, `verifier_flag_conflict`,
  alongside `verdict_scored_incorrect` and `passed_bad_terminal`. Consumers
  that switch on `basis` should treat it as **evidence, not a localisation**:
  it asserts no wrong verdict, and it never promotes a node to culprit or
  upgrades a report to `verification_gap`. Blaming a verifier on an explicitly
  unresolved conflict would trade one silent claim for a louder unfounded one.
  It carries no `score_overrides` entry for the same reason — the rubber-stamp
  override to 0.1 stays exclusive to `passed_bad_terminal`, where terminal
  ground truth actually refutes the PASS. The field is typed `string` in the
  web client and falls back to the raw slug, so an older UI renders it without
  breaking.

### Known, not fixed here

- **The heuristics channel still lifts the nodes the judge condemned hardest.**
  0.3.0 stopped `evaluate_heuristics` returning a flat 1.0 when no check fired
  (silence is not a verdict of health). The channel only ever subtracts from
  1.0, so when a check *does* fire it still lands high — and at 0.273 of the
  blend it pulls up every node scored below it. Measured on one live graph: a
  verifier the judge scored 0.0 came out at 0.216, and a collector scored 0.40
  came out at 0.554, i.e. over the 0.50 acceptance bar, on the strength of a
  0.034 repetition deduction. The worse the judged verdict, the larger the
  lift. The 0.3.0 note describes this failure mode exactly ("turned the absence
  of evidence into evidence of health"); it was closed for the silent case and
  is open for the fired-but-mild case. Fixing it will move numbers broadly and
  belongs in a MINOR, not here.

## [0.4.0] — 2026-07-30

`blame-engine` (0.3.0 → 0.4.0), `agent-detective` (0.3.0 → 0.4.0),
`agent-detective-worker` (0.3.0 → 0.4.0), `detective-sdk` (0.2.0 → 0.3.0),
`otel-mapper` (0.2.0 → 0.3.0), API and ingest services (self-host stack).

Verdicts change on unchanged traces. Three things move numbers you have seen
before: chain-shaped graphs discount attribution confidence, a cut_point
localised on a single scoring channel is capped at 0.7, and a well-formed empty
result is no longer scored as a defect. The exit-code contract (`0` / `1` / `2`)
is unchanged.

**Measured: the numbers held.** Unlike 0.3.0, this release does not claim an
accuracy gain — it is about reaching the tool at all, and the corpus exists
here to prove that making it usable did not cost anything. Over the same
foreign corpus (17 cells, 6 topologies, 5 runs each): attribution accuracy
**0.833 → 0.833**, false positive rate **0.20 → 0.20**, discrimination
0.927 → 0.909. Sixteen of the seventeen cells produce a byte-identical verdict
distribution to 0.3.0. The one that moved,
`05_diamond__hallucinate_at_marketing_writer`, went from 4 of 5 runs reporting
a fault to 5 of 5 — which removed the last unstable cell (1 → 0) and cost the
0.018 of discrimination, because that cell's clean twin reports the same
verdict, so agreeing with itself more consistently means discriminating less.

**Upgrade note.** Migrations 0014 and 0015 are additive and nullable, so a
0.3.0 deployment runs against the 0.4.0 schema unchanged; rollback is a
redeploy, not a down-migration. `agent-detective-worker` and `agent-detective`
require `blame-engine>=0.4.0` — the worker writes fields the engine reads, and
an unbounded partial upgrade would silently keep the old behaviour.

### Added
- **Rebuilt web UI.** A design-token system (`web/src/ui/tokens.css`) is the
  single source of colour, spacing and elevation, with an explicit light/dark
  theme written to `<html>` before first paint so a pre-JS render is never
  white-on-white; the graph canvas rebuilds its cytoscape stylesheet from the
  same signal. The screens are built from a shared primitive set — record
  lists, `Page`, `Toolbar`, `StatTile`, `Segmented`, `Drawer` — instead of
  tables, because the data this app shows (ids, agent names, judge prose) does
  not fit fixed columns and a table answers that by clipping. The app shell
  keeps the sidebar and page header fixed and scrolls only the body. Legacy
  `--bg` / `--text` / `--accent` names remain as aliases onto the new scale, so
  components written before the token system became theme-aware unchanged.
  A new **Contracts** screen registers an output contract from an agent's own
  stored payloads in one click, or shows why the samples do not support one.
- **Every score records what measured it.** `judge_model` is stored on the run
  whose judge component it produced and on both verdict tables beside the
  existing `judge_prompt_hash`; `/calibration` slices by the pair. Until now
  the model existed only in worker config and the outgoing HTTP request, so
  0.4 from a cheap model and 0.4 from a frontier one were the same row and
  `/calibration`, `/agents/leaderboard` and version-diff compared across
  incommensurable measurements with no way to tell (migration 0014).
- **Output contracts have a write path.** `GET`/`POST /contracts` and
  `GET /contracts/suggest`, plus `detective contracts {list,register,suggest}`.
  The table had no writer at all — 0 rows after `docker compose up`, readers
  only — so the schema scoring channel was unreachable without hand-written
  SQL, and the judge's weight silently covered the hole on every install.
  `suggest` derives a schema from the agent's own stored payloads: a key is
  required only when *every* usable sample carries it, types are the types
  observed, and nothing (enum, format, range, nested constraint) is invented.
  When the samples do not agree it returns a refusal with a reason rather than
  a permissive schema — one that passes everything would manufacture a
  0.35-weight channel out of payloads that never agreed on anything.
- **`agent_detective.node_kind`** — an optional span attribute declaring a step
  as `deterministic` or `tool`, carried SDK → mapper → ingest → worker
  (migration 0015). Such a node skips the judge entirely instead of being
  graded on an agent rubric it never ran under; role was inferred from the
  agent NAME alone, so a `plan_node` making zero LLM calls was handed the
  PLANNER rubric. The LangGraph adapter accepts explicit
  `node_kinds={"plan_node": "deterministic"}`; its auto-detection is opt-in
  (`detect_deterministic=True`) because "no LangChain callback fired" is not
  the same claim as "no model call happened".
- **`JUDGE_MAX_SPEND_USD`** with per-analysis judge spend accounting and
  logging. At the cap the worker stops calling and the remaining nodes come
  back unjudged through the existing unscored path — a partial analysis that
  says so beats a crashed one. Cost comes from the response's own usage when
  the endpoint reports it, else from an optional price table, else stays
  `null`: an unknown cost is never `$0`.
- **`POST /v1/traces` on the API port**, forwarding to ingest
  (`INGEST_BASE_URL`). Ingest listens on 8001 and the API on 8000, so a client
  told "the API is on 8000" — which it is — posted traces into a 404 with
  nothing to explain why.
- **Thread-safe event-driven step API** in the SDK (start/finish by name, join,
  fan-in), and a **LangGraph adapter** mapping nodes, `Send` fan-out and fan-in
  onto `step` / `branch` / `join`. Both existed as boilerplate every second
  integrator had to write.
- **LLM token usage captured from LangChain callbacks** onto the owning node.
  Tokens sum only when every call reported them, the model is recorded only
  when unambiguous, and USD only from an integrator-supplied price table
  covering every call — unknown stays absent, never 0.
- **`detective_sdk.run(trace_id=..., graph_id=...)`** so multi-stage pipelines
  share one execution graph without touching private attributes, and public
  **`Run.attr()`** symmetric to `Span.attr` for root-span attributes.
- **`GET /config` on ingest** and the effective configuration logged at
  startup: the quiescence window was discoverable only via `docker exec env`.
  `detective doctor` reads it and admits the value is unknown when no ingest
  answers, instead of assuming the default.
- `MappingResult.unresolved_delegations` — a TOOL_DELEGATION whose target
  agent name matched no run in the mapping call is recorded (owner run key,
  target name, trace id, span id) instead of vanishing. Additive field with
  a default; the `runs` / `edges` output is unchanged.
- `detective_sdk.run(parent_span_id=...)` parents the run root on a span from
  another process, so a pipeline layer handed work across process boundaries
  produces a structural SPAWN edge at re-map time instead of relying on
  name-resolved TOOL_DELEGATION alone. `Run.trace_id` / `Run.root_span_id`
  expose the identity to hand over. Invalid ids degrade to a root without a
  parent; `detective_sdk.otel.collect` / `root_span` accept the same option.
- `REANALYZE_LATE_SPANS` ingest flag (default off): a finalized graph that
  gained new runs goes through the full finalization path again — re-map over
  the complete span set, refinalize, exactly one new announcement on
  `ad.graphs.completed`. Worker-side dedup (`dedup_key=graph_id`,
  incident upsert on `(graph_id, incident_key)`) keeps the repeat safe.
- `MappingResult.unresolved_delegations` — a TOOL_DELEGATION whose target
  agent name matched no run in the mapping call is recorded (owner run key,
  target name, trace id, span id) instead of vanishing. Additive field with
  a default; the `runs` / `edges` output is unchanged.
- `detective_sdk.run(parent_span_id=...)` parents the run root on a span from
  another process, so a pipeline layer handed work across process boundaries
  produces a structural SPAWN edge at re-map time instead of relying on
  name-resolved TOOL_DELEGATION alone. `Run.trace_id` / `Run.root_span_id`
  expose the identity to hand over. Invalid ids degrade to a root without a
  parent; `detective_sdk.otel.collect` / `root_span` accept the same option.
- `REANALYZE_LATE_SPANS` ingest flag (default off): a finalized graph that
  gained new runs goes through the full finalization path again — re-map over
  the complete span set, refinalize, exactly one new announcement on
  `ad.graphs.completed`. Worker-side dedup (`dedup_key=graph_id`,
  incident upsert on `(graph_id, incident_key)`) keeps the repeat safe.

### Changed
- **A score reports the weights it was actually blended from.**
  `composite_score` returns `CompositeScore(score, unscored_reason,
  effective_weights)` — the arithmetic is unchanged, what changes is that the
  renormalization is no longer silent. A channel that never reported handed its
  weight to the ones that did (schema absent ⇒ the judge's 0.40 becomes 0.727
  of the blend) while the report still read "weighted mean over three
  independent components". The effective weights are stored on the run
  (migration 0014), served by the API, and shown by the CLI as
  `1 of 3 channels (effective weight: …)`. **Breaking** for direct callers of
  `composite_score`, which used to return a 2-tuple.
- **A missing channel now lowers confidence instead of promoting the survivor.**
  Attribution is discounted when the origin's score rests on fewer channels
  than were weighed, and a `cut_point` localised on a single channel is capped
  at 0.7 — naming one origin on one instrument's word cannot reach the
  certainty of a localisation two independent channels agree on. The verdict
  *type* is unchanged: a measured drop still happened there, and every weaker
  report type asserts something the evidence does not say.
- **Attribution confidence accounts for graph shape.** On a chain-shaped graph
  every interior node is an articulation point, so "this node is the cut point"
  rests on ordering rather than structure. The discount scales with the number
  of nodes the shape cannot distinguish (×0.95 at depth 3, ×0.80 at depth 18
  and beyond) rather than being flat, because three steps still narrow the
  origin to one interior node and eighteen narrow it to seventeen. Observation
  confidence is untouched — whether an output is defective does not depend on
  the graph's shape.
- **A tie between equally evidenced origins is reported as a tie.** Several
  nodes on the same score used to be resolved into one name by the tie-break
  order; the competing origins now appear in `Evidence.hypotheses` with an
  explicit unresolved remainder. The named culprit and the ordering are
  unchanged — what changes is that the alternatives stop being invisible.
- **Costs travel with their coverage.** `Evidence.cost_coverage`
  (`{"priced": n, "total": m}`) on the report and `priced_run_count` on graph
  and agent aggregates. A total summed over 6 of 28 priced runs is a lower
  bound; printed bare it read as the price of the run, and the incident inbox
  summed such totals treating an unpriced run as a free one.
- **The judge is told what the deterministic channel already knows.** The
  per-node prompt carries a delimited `DETERMINISTIC FACTS` block — contract
  parameters the node rewrote, artifact visibility, every fired check — plus
  how many sibling nodes of the same run also returned a well-formed empty
  result. The judge was guessing at facts the same function had already
  established, and a lone collector that found nothing looks negligent until
  you know four siblings found nothing either.
- **The worker states which judge it is using at startup** — model, base URL,
  and whether the verdicts are canned mock answers or a real model.
- New `unscored_reason` values: `zero_result_set`,
  `judge_skipped_deterministic_node` (the trace declared the node
  deterministic, so the judge was deliberately not run) and
  `judge_budget_exhausted` (the spend cap bound). The last two are deliberate
  outcomes, not faults; an exhausted budget used to be indistinguishable from
  an unreachable judge.
- **New judge role: RETRIEVER / COLLECTOR.** `blame_engine.roles` gained
  `RETRIEVER_PREFIXES` + `is_retriever` (prefix-token matching, same discipline
  as the planner hints), and `node_role()` resolves it with explicit precedence
  VERIFIER → PLANNER → DELIVERABLE PRODUCER → RETRIEVER → INTERMEDIATE. A
  retriever's correct output is a faithful report of what the source returned —
  including nothing — so collectors with partial results are judged on the
  query and the fidelity of the report, never the yield. `harvest` and
  `search`/`lookup` stay out of the stem set on purpose (scored-on-yield and
  tool-name collisions respectively); a name with no hint still falls through
  to INTERMEDIATE. The judge rubric now also states that a plan may be
  structured data — a routing object like `{ico, sources}` IS the plan — with
  a worked example mirroring the prose-outline one.
- **A run no analysis ever covered now reads `not_analyzed`, distinct from an
  analysed-but-unscored run.** Both used to surface as `quality_score=null` +
  `unscored_reason=null`; the API graph detail and the CLI report now derive
  `not_analyzed` from that pair at the read boundary (the engine never writes
  it), and the CLI report leads the Pipeline listing with a per-state node
  count (`node states: 3 scored · 2 not_analyzed · …`). Runs the analysis did
  score keep their original `unscored_reason`
  (`payload_missing` / `empty_output` / `insufficient_components` /
  `judge_error`). Blame-engine inputs are unchanged.

### Fixed
- **A well-formed output carrying no records is unscored, not a defect.** Such
  a node was handed to the judge, which rates emptiness as worthless; eight of
  them on one graph deflated the median enough to fabricate a `multi_culprit`
  verdict out of an observed absence. A `zero_result_set` gate now runs before
  the judge — skipped for failed runs and errored tool digests, where the
  emptiness may be the failure's own footprint — and the observation rides the
  deterministic channel as a warn signal. The engine treats the new reason like
  `empty_output`: observed, not a blind spot, so it neither blocks
  classification nor caps confidence as an unknown ancestor.
- **A run no analysis ever covered reads `not_analyzed`.** `quality_score=NULL`
  + `unscored_reason=NULL` meant both "never analysed" and "analysed, no
  score". The engine never writes that pair, so the read boundary derives it;
  the CLI report leads its Pipeline listing with a per-state node count.
- **An offline run says why nothing was scored.** Without a judge every node
  ended `insufficient_components` with nothing stating the rule, so a
  deliberate design read as a malfunction. `detective doctor` now names the
  score floor before the analysis runs, and unscored nodes that carry real
  measurements show them labelled as a partial deterministic measurement.
- **Findings export no longer pairs a judge sentence with the composite
  score.** The judge-findings section of the exported Markdown brief showed
  `score_map`'s blended `quality_score` (schema + judge + heuristics) next to
  the judge's own note, making the judge look self-contradictory. Each note
  now carries the judge's own `score_components.judge`; when it differs from
  the composite both numbers print with the claimed → effective vocabulary,
  and when the judge component is missing (unscored node, judge never ran)
  the export says so instead of borrowing the composite. The verdict block
  labels its score as composite, and unscored nodes in the Node quality table
  print their `unscored_reason` rather than a number.
- **A delegation split across POSTs of one trace now produces its edge.** The
  ingest finalization re-map retries the delegations it still could not
  resolve against the graph's stored runs, so the TOOL_DELEGATION edge is
  closed even when the target layer's raw spans are unavailable at remap
  time. A delegation whose target exists nowhere in the graph still yields
  no edge — endpoints are never invented — but now leaves one warning per
  graph naming the missing target.
- **Spans arriving after finalization are no longer silently absorbed.** The
  ingest upsert detects (inside its own transaction) that a batch touches a
  finalized graph, logs one warning per graph per POST, and stamps the graph
  with `late_spans_count` / `late_spans_last_at` (migration 0013, additive
  and nullable), readable via `GET /graphs` and `GET /graphs/{id}`. Only
  genuinely new runs count, so redelivery changes nothing.

## [0.3.0] — 2026-07-29

`agent-detective`, `agent-detective-worker`, `blame-engine`. Verdicts change on
unchanged traces — that is the point of the release.

Measured on the foreign corpus (18 cells, 6 topologies, 5 runs each):
attribution accuracy **0.43 → 0.833**, discrimination 0.58 → 0.93, false
positive rate 0.24 → 0.20.

### Added
- **Numeric fidelity**, the deterministic answer to "fluent but wrong".
  `number_not_derivable` catches a figure in a tabular output that is neither
  present in the input nor reachable from one by a rate the input states;
  `numeric_content_lost` catches an output that dropped every figure it was
  handed. No model in either. This is what moved attribution.
- `run_failed` — the trace records a run as errored. That fact used to reach the
  verdict as nothing: its only landing place was the heuristics component, whose
  weight does not clear the scoring floor alone, so a graph containing a crashed
  node was reported INCONCLUSIVE at 0% confidence.
- `worker/payload.py` — one typed view per payload, built once. Every check used
  to re-parse the raw text and decide for itself whether a comma separates
  fields or marks a decimal; getting that wrong does not make a check miss, it
  makes it accuse.
- `JUDGE_GATE` (off by default) — run the deterministic half first and skip the
  per-node judged pass when it already localised a defect whose origin it
  observed.

### Fixed
- **The judge's reasoning now travels with the number it explains.** Judged
  findings carried `{agent, score}` and nothing else, so a report said "plan
  scored 0.56" with no way to learn why. The text existed the whole time.
- A deterministic-only localisation reported 0% confidence while citing a
  100%-certainty finding: two predicates for "this node has a hard deterministic
  defect" had drifted apart, and the one driving confidence read a closed set of
  three flag names.
- `evaluate_heuristics` returned a full 1.0 when none of its checks fired, and
  the composite averaged that in as positive evidence of quality — a constant
  worth ~27% of every score, which undid the judge's flag caps.

### Changed
- A failed judge plus a passing schema contract no longer clears the scoring
  floor on its own. Conforming JSON and no complaints is not a quality
  measurement, so those nodes are unscored rather than carrying a number nothing
  measured.

## [0.2.2] — 2026-07-29

Self-hosted stack only; no distribution changed. `agent-detective` stays at
0.2.1 on PyPI.

### Changed
- **Compose binds every published port to `127.0.0.1` by default** — the UI and
  API, and Postgres, ClickHouse, Redis and MinIO with them. The stack ships
  without authentication over a database of verbatim agent payloads, so
  "reachable from the network" is now a decision (`BIND=0.0.0.0 docker compose
  up`) rather than what happens if you do nothing on a host with a public
  interface. Container-to-container traffic is unaffected — services reach each
  other by name on the compose network, never through a host binding.

  This matches what `detective capture` already did: loopback unless you pass
  `--host 0.0.0.0` on purpose.

## [0.2.1] — 2026-07-29

`agent-detective` only. Documentation, no behaviour change.

### Added
- Beta notice in both READMEs, stating precisely what is unstable (verdict
  wording) and what is not (exit codes), with 0.2.0's reclassification as the
  worked example.
- `CONTRIBUTING.md` — workspace layout, how to run the suites the way CI does,
  and the house rules that are load-bearing rather than stylistic (absent
  evidence never becomes a number; decision code emits typed records, never
  prose).
- `CHANGELOG.md` (this file).
- README now says out loud that the self-hosted stack ships **without
  authentication** and publishes its ports on every interface, over a database
  holding verbatim agent payloads.

## [0.2.0] — 2026-07-29

`agent-detective`, `agent-detective-worker`, `blame-engine`, `detective-sdk`,
`otel-mapper`. Found by running a real nested-loop topology through the CLI:
nothing in it ran away, and the report named all 21 of its nodes.

**Verdicts change on unchanged traces.** A run previously reported as
`loop_detected` at 100% confidence can now classify differently — that is the
point of the release, not a regression.

### Fixed
- **The loop check counted cycle size, not rounds.** `max_loop_iterations`
  bounds iterations, but the check read the condensed SCC's member count: a
  bounded 2×3 nested loop condenses to 21 members and was reported as 21 runaway
  iterations. The mapper now carries `agent_detective.attempt` /
  `.attempt_of` (previously emitted by the SDK and read by nothing) through to
  the engine, which groups by agent and counts the busiest group. Traces without
  those attributes keep the member-count fallback, unchanged.
- **A loop anomaly blamed every member of the cycle**, controllers and bystanders
  included. Only the runs that actually repeated are named now.
- **An empty output with spent tokens was reported as an exporter defect.** The
  exporter had worked; it captured an agent that spent its budget and returned
  nothing. A payload *recorded* empty (`""`, not absent) with
  `gen_ai.usage.output_tokens > 0` now raises a fail-severity `empty_output`
  signal and its own unscored reason, and stops counting as an unknown that
  could be hiding a culprit. The score still stays UNKNOWN — emptiness is never
  graded `0.0`.
- **Loop baselines were looked up under per-attempt agent names** (`builder#7`),
  which occur exactly once, so a baseline recorded for `builder` never matched.
- **`detective_sdk` could not express a loop running beside another arm.**
  `retry()` took its parent from the innermost open span, so two threaded loops
  became each other's parent — an edge no execution performed. The open-span
  stack is also now lock-guarded: its rebuild on close raced appends from
  parallel arms.

### Added
- `detective_sdk`: `Run.retry(parallel=True)` for a loop that is one arm of a
  fan-out, and `of=` to name the dispatching step explicitly.
- Migration `0012`: `agent_runs.attempt` / `.attempt_of`, carried by ingest and
  read by the worker, so the fix holds on the deployed path and not only in
  local CLI mode.

### Changed
- `agent-detective` and `agent-detective-worker` now declare `>=0.2.0` lower
  bounds on their internal dependencies. The fix spans packages — the mapper has
  to emit what the engine reads — so an unbounded partial upgrade would silently
  restore the old false positive instead of failing.

## [0.1.0] — 2026-07-26

First public release: local-mode CLI (`detective analyze` / `capture` /
`doctor`) running the deployed service's own tier1/tier2 processors against
in-memory seams. Deterministic channel by default, judge opt-in, `NOT VERIFIED`
when nothing could be measured.
