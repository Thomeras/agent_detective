# Roadmap

Agent Detective is a universal agent-observability platform: OTEL-native in,
graph-first analysis, blame out. Nothing below assumes a particular agent
framework — every check is defined as a platform convention (attribute names
per `docs/instrumentation.md`; integrity metadata travels out-of-band as span
attributes, never as forgeable payload text) plus worker-side parsing of that
convention. `generative_simon` is only the reference integration.

This document maps the remaining work onto the **deterministic-signal
framework** defined in `docs/deterministic-signals.md`: every new check emits a
named signal `{name, run_id, severity, detail, basis, provenance}` into
`NodeScore.deterministic_signals` / `Evidence.deterministic_signals`
(node-level) or is appended by the worker post-serialize (graph-level). The A1
(artifact integrity) and B1 (versioning) items from that design are the current
iteration and are treated as the foundation here, including the
`packages/detective_sdk` helper package it introduces.

Effort scale: **S** ≈ a day, **M** ≈ a few days, **L** ≈ a week-plus including
schema or SDK surface changes.

## 1. Deterministic check library (signal framework extensions)

Placement rule: anything that needs host/file access runs in the
**instrumentation** (via `detective_sdk` helpers, shipped as span attributes);
anything computable from payloads runs in the **worker** — graph-level always-on checks
in tier1 (`worker/tier1.py`), per-node checks in tier2 scoring
(`worker/scoring.py`), post-blame enrichment in `worker/tier2.py`.

### 1a. Required sections / content extraction against the brief

| Signal | Runs where | Needs | Effort |
|---|---|---|---|
| `missing_required_section` | tier2 scoring (per node), tier1 (deliverable) | registered content contract per graph_type/agent: required headings, required fields, per-requirement regex — an extension of the existing `output_contracts` table (add a `content_requirements` JSONB) or a sibling table; deliverable text is already resolvable (`resolve_payload`, incl. embedded `artifact_text`) | M |

Catches: a deliverable that silently omits sections the brief demanded. Today
this exists only as the LLM-declared `missing_required_content` flag, which
`_JUDGE_FLAG_CAPS` caps at 0.55 (`worker/scoring.py`) — useful, but the judge
must first *admit* the omission. A registered requirement list makes the check
deterministic: extraction is a heading/regex match, the `basis` names the
missing requirement verbatim, and the signal caps the score whether or not the
judge noticed.

### 1b. Numeric & structural invariants

| Signal | Runs where | Needs | Effort |
|---|---|---|---|
| `numeric_invariant_breach` (sum invariants) | tier2 scoring | invariant rules registered per agent/graph_type (e.g. "sum(items[].price) == total ± tolerance"), stored beside `output_contracts.json_schema`; JSON-parseable outputs | M |
| `unit_inconsistency` (currency/units) | tier2 scoring | payloads only: scan for mixed ISO currency codes / unit tokens bound to the same field between a node's input and output | M |
| `structured_field_drop` (hashed field propagation) | tier2 post-blame enrichment | payloads only: extract scalar leaves from a node's JSON output, hash normalized values (reuse `_norm` from `worker/scoring.py`), test presence in each downstream payload along edges | M |
| `temporal_invariant_breach` | tier2 scoring | payloads + a few built-in rules (end ≥ start, no dates after the run's own end time); optional registered date-field rules | S |
| `language_mismatch` | tier2 scoring | `langdetect` (small, pure-Python) in the worker; the expected language already travels through the contract keys `lang`/`language`/`locale` in `_CONTRACT_KEYS` | S |

`structured_field_drop` deserves emphasis: it is the **deterministic
replacement for fuzzy fact propagation**. Today `worker/tier2.py::_fact_propagation`
asks the judge (`prompts/claims.md`) to extract the culprit's claims and greps
downstream payloads for them — an LLM extraction step. The existing
`contract_propagation_check` (tier2.py) already proves the pattern
deterministically for rewritten contract params (param match / artifact-path
extension, no free-text scan); `structured_field_drop` generalizes it to all
structured fields: which fields died at which edge, no LLM in the loop. The
claims-based pass demotes to a fallback for prose-only payloads.

`language_mismatch` catches the archetypal silent wedge failure — a translator
emitting the wrong language while schema and a fluent judge both pass — for the
cost of one library call.

### 1c. Behavioral trace signals

| Signal | Runs where | Needs | Effort |
|---|---|---|---|
| `loop_fingerprint` | tier1 (graph-level) | per-iteration content: hash of normalized member outputs and/or tool-call args per loop iteration; tool-call args are currently only in ClickHouse `otel_spans`, not on `agent_runs` — either query ClickHouse from the worker or extend `packages/otel_mapper` to persist a per-run tool-call digest | M–L |
| `retry_storm` | tier1 | already-loaded bundle data: run statuses, same-agent retry spans (scoring already receives `retry_count`) | S |
| `tool_args_invalid` | instrumentation (`detective_sdk`, sees the live tool schemas) or tier2 with schemas registered like `output_contracts` | tool JSON schemas + tool-call args | M |
| `cost_anomaly` / `token_anomaly` | tier1 | rolling per-agent baselines. The `agent_stats` table exists (PK `(agent_name, graph_type)`, `tokens_out_mean/std`, `iterations_mean/std`, `sample_count`) and both tiers read it (`read_agent_stats`), but **nothing in the worker writes it** — the demo seeds it. Work: a post-tier1 rolling updater (Welford), `cost_mean/std` columns (migration), z-score signal | M |
| `duplicate_side_effect` | tier1 | tool-call data (same dependency as `loop_fingerprint`) + a small registry of side-effecting tool names (send/post/pay/write) | M (L incl. tool-call persistence) |

What the existing `loop_anomaly` already covers, precisely
(`blame_engine/loops.py`): a condensed SCC is anomalous only by iteration
**count** — `iterations > max_loop_iterations`, or a statistical outlier
against the dominant agent's `agent_stats` baseline. What is missing: a loop
that spins four *identical* iterations under the limit is invisible. The
fingerprint check hashes each iteration's content; N identical consecutive
fingerprints is a degenerate cycle regardless of the cap — a stuck agent
burning budget with zero progress.

`cost_anomaly` matters because the only cost check today is the fixed
`COST_BUDGET_DEFAULT_USD` (default `None`, i.e. disabled) behind
`FLAG_COST_OVERRUN` in tier1 — a static line, not a regression detector.
`duplicate_side_effect` catches the failure class quality scores cannot see at
all: the email sent twice, the payment posted twice.

### 1d. Security (enterprise-gated, runs locally)

| Signal | Runs where | Needs | Effort |
|---|---|---|---|
| `sensitive_data_exposure` | both: `detective_sdk` pre-export redaction hook (best — the data never leaves the host) and tier1 always-on payload scan | regex battery (email, phone, IBAN, card+Luhn, cloud keys, JWT, private-key headers) + Shannon-entropy scan for high-entropy tokens | S scan, M redaction hook |
| `prompt_injection_signature` | tier1, over TOOL-span outputs and externally-sourced node inputs | signature list: instruction-override phrases, zero-width/bidi unicode, markdown-image exfiltration links, role-tag smuggling | M |

Both are deterministic (regex + entropy — no third-party calls, everything runs
inside the deployment), which is what makes them shippable to enterprises at
all. A confirmed injection signature in a tool output gives
`root_cause_external` a concrete `basis` instead of an inference: the fault
verifiably entered from outside the graph.

## 2. Deploy-vital platform features

In priority order. Each sketch names the actual tables, streams and services.

### 2.1 Versioning + config diff views

B1 lands `model_name` and `prompt_hash` columns on `agent_runs` beside the
existing `agent_version` (migration 0006, in flight per
`docs/deterministic-signals.md`). The data answers "why did it work yesterday";
this feature is the view over it:

- **Diff UX**: on a blame report, the web UI (`BlameReportPanel`) shows a
  version diff between this graph and the most recent blame-free graph of the
  same `graph_type` — per agent: `(agent_version, model_name, prompt_hash)`
  changed/unchanged. Backing query is a join over `agent_runs` ×
  `blame_reports` (`is_latest`) × `incidents`; a
  `GET /graphs/{id}/version-diff?against=<graph_id|last_clean>` endpoint in
  `services/api/api/routers/graphs.py`.
- **Leaderboard per version**: `GET /agents/leaderboard`
  (`routers/agents.py`) gains a `group_by=version` mode — blame rate and
  incident count per `(agent_name, agent_version, model_name, prompt_hash)`
  tuple instead of per agent.

Pure SQL + API + UI; no new tables. Effort: S–M. This is first because it is
the cheapest feature with the highest "first question an operator asks" value,
and because 2.4 depends on it.

### 2.2 Policy gates in trace (shadow mode)

Where policy decisions live in the schema: a `policy_rules` table (name,
predicate over deterministic signal names / tier1 flags / score thresholds,
action `warn|block`, `shadow` bool) and a `policy_decisions` table
(`graph_id`, nullable `run_id`, rule name, decision, `mode='shadow'`,
timestamp). Evaluation point: tier1, immediately after flags and graph-level
signals are computed and before the `tier1_verdicts` upsert — the decision set
is serialized into the verdict row so the UI can show "this run would have been
blocked by rule X" in the incident inbox.

Honesty requirement: Agent Detective analyzes *after* the fact. A shadow gate
is an annotation, not an interception — the doc and UI must say "would have
blocked", never "blocked". Enforcement is 2.3's problem. Effort: M.

### 2.3 Circuit breaker / kill switch

The honest architecture statement first: **Agent Detective observes; it cannot
stop anything.** Enforcement requires a control hook on the instrumented side,
which only exists if the integration adopts it.

Sketch: when a breaker rule trips (e.g. `loop_fingerprint` fail, cost z-score
breach, N open incidents for one `agent_version` inside a window), tier2/tier1
publishes to a new stream `ad.control.signals` and upserts a `breaker_state`
table (scope: agent_name or agent_version; state open/closed; reason = signal
name). `services/api` exposes `GET /control/breakers`. On the agent side,
`detective_sdk` offers an *optional* `should_halt(agent_name, graph_id)` poller
against that endpoint; the agent decides to honor it. Integrations that do not
adopt the hook get exactly nothing — the feature documentation must state this
plainly rather than implying a kill switch that does not exist. Effort: L
end-to-end.

### 2.4 Canary / shadow deploy comparisons

Enabled by 2.1: blame-free rate per `(agent_version, prompt_hash, model_name)`
computed from `execution_graphs` × `agent_runs` × `incidents`. API:
`GET /agents/{name}/versions/compare?base=&candidate=`; UI: two-column
comparison with a per-signal breakdown — which deterministic signals fired in
the candidate but not the base (this is where named signals pay off: the diff
is a set of stable identifiers, not two piles of prose).

Coverage caveat: full blame data exists only for flagged or sampled graphs and
`TIER2_SAMPLE_PCT` defaults to 0. The honest v1 compares **tier1** rates
(always-on, one cheap judge call per graph): flag rates and terminal-verdict
rates per version from `tier1_verdicts`. Deep comparison requires sampling a
nonzero percentage for the canary cohort. Effort: M.

### 2.5 Golden snapshots / regression diff + CI gate

Verified: **no pytest plugin exists today.** `packages/` contains only
`blame_engine` and `otel_mapper`; CI is `.github/workflows/ci.yml` (unit suites
+ the end-to-end acceptance test) and `scripts/test.sh`. This is a new
deliverable, not an extension.

Sketch — `detective-ci`, a pytest plugin or CLI that: (a) replays a recorded
OTLP fixture (the JSON shape in `packages/otel_mapper/testdata`) into a compose
stack or an in-process ingest→tier1→tier2 pipeline; (b) waits for the
`blame_reports` row; (c) diffs against a golden snapshot on the **stable**
surface only — `report_type`, culprit agent names, fired deterministic signal
names — never confidences to the third decimal (LLM judge scores are not
reproducible; deterministic signals are, which is exactly why they anchor the
snapshot); (d) fails the build on regression. The unused `checkpoints` table
(`run_id`, `input_ref`, `model_config`, `tool_calls` — prepared for replay
since migration 0001) is the natural place for recorded fixtures to graduate
into per-node replay later. Effort: L.

### 2.6 Audit trail non-repudiation

`blame_reports.evidence` is mutable JSONB and incidents can be superseded
(migration 0005). For deployments where a blame verdict has consequences, add
an append-only `evidence_ledger` table: `report_id`, sha256 over the
canonically-serialized evidence, `prev_hash` (chain), signature (HMAC with a
server key in v1; asymmetric keys later). Written inside
`persist_tier2_result` at the moment the report is persisted — cheap, because
evidence is already serialized deterministically in exactly one place
(`worker/tier2.py::serialize_evidence`). API endpoint to verify a report
against the chain. Effort: M.

### 2.7 Production feedback loop

The API already has `PATCH /incidents/{incident_id}` for status changes; extend
the surface with verdict feedback: thumbs-down on a tier1 terminal verdict or a
blame report, with an optional "actual culprit" pick, lands in a
`ground_truth_labels` table (`graph_id`, label ok/bad, optional
`culprit_run_id`, `source='human'`, timestamp). Two consumers: a calibration
report (terminal-judge precision/recall against labels, shown per judge model
in the UI) and a growing eval fixture set for judge-prompt changes. One gap to
close on the way: B1's `prompt_hash` identifies the *agent's* prompts, not the
judge's — the worker should stamp its own judge-prompt hash (over
`worker/prompts/*.md`) onto `tier1_verdicts` and `blame_reports` so calibration
can be sliced by judge-prompt version. Effort: M.

## 3. Principles

**Deterministic beats LLM.** Where a fact is checkable without a model, the
check overrides the model: a confirmed contract violation forces the node score
to ≤ 0.15 regardless of a fluent judge verdict (`worker/scoring.py`,
`_CONTRACT_VIOLATION_SCORE`), structured judge flags cap the judge's own number
(`_JUDGE_FLAG_CAPS`), and tier1 overrides a confident LLM verdict to
`not_checkable` when the deliverable was never actually in the payload
(`tier1.py::_terminal_judge`). Every check family in section 1 follows this
precedence.

**Named signals with provenance.** Checks emit stable identifiers, not prose:
tier1 flags are constants (`FLAG_LOOP_ANOMALY`, … in `worker/types.py`),
contract violations travel as their own structured stream
(`NodeScore.contract_violations` — deliberately not glued into the judge note),
and the signal shape in `docs/deterministic-signals.md` requires a `basis`
stating the observation each signal rests on. Dashboards, gates and diffs key
off names; humans read the detail.

**Honest confidence.** A fallback verdict is never sold as certainty:
`blame.py::_CONFIDENCE_CAP` ceilings each report type (`composition_failure`
≤ 0.4, `verification_gap` ≤ 0.6, …), `Evidence` splits
`observation_confidence` ("the output is defective") from
`attribution_confidence` ("it originated here"), and assumed-baseline drops are
excluded from evidence rather than presented as measurements
(`Candidate.base_assumed` in `cutpoint.py`). New features inherit the rule —
see the "would have blocked" and tier1-only-canary caveats above.

**Evidence-first.** No claim without its observable: the per-node `candidacy`
trace carries the actual numbers behind every inclusion/exclusion, "terminal is
bad" always ships the tier1 judge's verdict and reasoning alongside
(`Evidence.terminal_verdict`), and "the defect reached the deliverable" is
asserted only after `contract_propagation_check` verifies it in the deliverable
payload (`worker/tier2.py`). The audit ledger (2.6) and golden snapshots (2.5)
extend the same stance to time: evidence that cannot be silently rewritten, and
verdicts that cannot silently drift.
