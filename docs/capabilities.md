# Agent Detective — current capabilities

Snapshot 2026-07-24 (post defect-evidence discipline; 652 unit tests green).
Companion docs: `architecture.md` (system design), `trace-requirements.md`
(what the trace must carry), `deterministic-signals.md` (signal catalogue),
`usage.md` (how to use all of it).

**Job:** forensic analysis of multi-agent runs from OTEL traces. Answers, in
order: did it pass? — if not, where, what kind of fault, and how sure are we?
No access to the agent system; trace content only.

## Ingest & reliability
- OTEL spans → execution graph (nodes, edges, payloads, artifact meta, cost);
  `graph_type` from `service.name`. OTLP/HTTP in BOTH wire formats (JSON and
  protobuf), identical rows either way.
- Finalization re-map: before a graph is announced complete, the full stored
  span set is re-mapped (cross-batch edges, late roots, trailing identity
  spans recovered) — standard timer-flushed exporters are safe.
- Redis streams with orphaned-pending reclaim (an analysis never silently
  disappears), idempotent processing, versioned reports, graph-level
  supersede of stale incidents; an analysis landing back on a superseded/
  resolved incident key REOPENS it (acknowledged stays with its human).

## Two evidence channels
- **Deterministic** (certainty 1.0, reproducible rules): contract breaches
  from input/output diffs of carried parameters (docx→md) — the input side
  declarable out-of-band via the `agent_detective.contract_params` span
  attribute (SDK: `span.contract(file_type="pdf")`), so prose/code pipelines
  get the check without payload conventions; named signals
  (missing_required_section, artifact integrity, retry storms, …); loop
  anomalies; verification of breach PROPAGATION into the shipped artifact
  (contract param / artifact-path extension → `breach_propagated` /
  `breach_corrected`).
- **Judged** (calibrated certainty): per-node quality judge; role-aware
  verifier judging (PASS/FAIL correctness, not artifact quality); terminal
  judge with a SPLIT rubric — content vs. form, the form verdict carrying the
  verbatim requirement quote from the initial input (UserRequest provenance).

## Reconcile — no two records of one fact without a verdict between them
Findings measuring the same fact share a `fact_key`; disagreement emits a
typed divergence: `representation_divergence` (producers have the section,
the deliverable lost it), `requirement_provenance` (user ask "jako PDF" vs.
contract scaffold docx, via file-type token normalization),
`assessment_conflict` (terminal-review verifier claims the work flawed while
the checkable terminal says ok — the typed form of judge confabulation).

## Verdict (schema 2: Finding → Defect → Projection)
- Typed findings with provenance (rule fingerprint / judge prompt /
  requirement quote / harness / upstream) and calibrated certainty.
- Defects with an **origin sum type** (Localized / Unlocalized-with-reason /
  External / Design), channel DERIVED from supporting evidence, and refs with
  polarity (supporting / refuting / context) under a validator: no defect
  without a supporting finding, no propagation claim without a cited
  propagation finding.
- `report_type` purely derived from the defects (cut_point, multi_culprit,
  degraded_recovered, in-engine escalation to shipped_with_latent_defect,
  terminal_defect_unlocalized, composition_failure, verification_gap,
  loop_detected, root_cause_external). A verdict cannot disagree with its own
  evidence.
- Honest confidence: an observation/attribution pair with structural caps;
  caveats as fields (base_assumed, observability_boundary,
  unverified_in_channel, recovered) rendered as unclippable chips.
- Incidents: latent_defect (silent defect shipped to production) >
  degraded_quality > terminal_failure > cost_overrun; alerting reads typed
  fields, never prose.

## UI
Verdict-first runs list; run detail with defect cards (evidence /
counter-evidence / context groups, observation+attribution meters, caveat
chips), graph canvas with culprit ring, policy shadow, versions, raw
evidence; human ground-truth feedback with judge-vs-label calibration.

## Validation on record
652 hermetic tests + golden fixtures; a labelled corpus of 4 cells (injected
md-rewrite ×3 + the proposal trace) with judge cassettes incl. environment
provenance (model, endpoint, temp, prompt hashes); a 20-run variance
measurement: the structured judged layer was 20/20 byte-stable on this stack
while only prose floated — historical "flaky" verdicts were judge-prompt
VERSION artifacts (cross-pass comparisons gate on `judge_prompt_hash`). A
documented triad of LLM-judge failures (confabulation / miscalibration /
misreference), each caught by the deterministic counter-channel.

## Honest limits
What the trace did not capture at run time, no analysis can manufacture
(`trace-requirements.md`). Without a judge, most nodes get no quality score
unless a registered output contract crosses the weight floor — the exact
arithmetic and every `unscored_reason`: `usage.md` §4.4. The corpus is still
ONE harness, ONE linear topology, ONE injection — the graph-first thesis
(fan-out, retry loops, A2A) awaits its validating trace. Backlog:
orphan-findings counter, the text-vs-own-score half of the assessment
emitter, inline-prose deletion, B2 screens.
