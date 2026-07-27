# Trace requirements — what must be captured at RUN TIME for the detective to claim X

Seed (2026-07-24), grown from a corpus lesson. Living doc: every time an
analysis cannot make a claim because the trace lacks something, that becomes a
row here — this is the product's honest dependency surface, and the wall a
customer with thin OTEL spans will hit.

## The principle

**What instrumentation did not capture at run time, no later analysis can
manufacture.** Reprocessing replays *interpretation* over stored evidence; it
never adds evidence. The proposal-trace corpus cell proved it concretely: its
tier1 verdict predates the terminal rubric split, so it carries no form
dimension — and a tier2-only reprocess through the current engine can emit
neither the form defect nor the requirement (pdf) vs contract (docx)
divergence that the A/B/C cells surface on the *same injected fault*. The
evidence gap is permanent for that trace.

Corollary for verdict semantics: an absent claim is `unverified`, never `ok`.
The engine's caveat fields (`unverified_in_channel`, `observability_boundary`,
`base_assumed`) exist to say this out loud.

## Claim → required trace capture (first rows)

| To claim… | The trace must carry, at run time… | Without it |
|---|---|---|
| form defect / "shipped form ≠ requested form" | terminal FORM verdict (split rubric) incl. the verbatim requirement quote | no form defect, no requirement reconcile (proposal cell) |
| requirement divergence (user ask vs contract scaffold) | the requirement quote (UserRequest provenance) AND the contract reference value | both values print silently side by side — the pre-split docx/PDF/md triple |
| breach propagated / latent defect shipped | deliverable producer's payload (or structured artifact path) | propagation `unverified` — escalation cannot fire, caveat stays |
| representation divergence (producers have it, deliverable lost it) | producer output payloads + deliverable payload | "found in act" and "missing" print unreconciled (report-#1 class) |
| content localization (cut point, drops) | per-node payloads (judgeable) + node ordering/edges | nodes unscored → observability boundary, `base_assumed` |
| loop anomaly | run/span identity stable across retries + iteration counts | invisible |
| verifier verdict correctness | the verifier's verdict AND the artifact it reviewed | role-aware judging degenerates to guessing |
| cross-pass score comparison | judge prompt hashes (tier1 `judge_prompt_hash`, check_rules hash) | scores are not comparable — prompt-version changes masquerade as flakiness (20-run variance lesson) |

## Measured on a foreign trace (CrewAI cell 1, 2026-07-24)

The first non-our-harness cell (vanilla OpenInference CrewAI) turned rows
above into observations — same injected fault class the linear
corpus escalates (silent requirement rewrite) was honestly UNDETECTABLE:

| Gap observed | Consequence | Cheapest lever |
|---|---|---|
| no `gen_ai.agent.name` (identity only in span names / config JSON) | agent_name NULL → verifier roles never engage | ~~mapper fallback: parse span name~~ **LANDED** (CrewAI profile, signature-gated) |
| task→task data flow absent (all spans parent on kickoff) | ZERO edges → no topology, no propagation, no shadowing | ~~heuristic~~ **LANDED**: `crewai_sequential` rule — Process.sequential guarantees the chain, derived from sibling order |
| input/output.value wrap task metadata JSON | judges see blobs; 2/3 nodes unscored | ~~mapper unwrap~~ **LANDED** (description/raw unwrap). Live validation: 3/3 named, 3/3 scored, 2/2 edges |
| ingest maps per OTLP request, never re-maps | batch splits lose cross-batch edges; completion can fire mid-trace → partial analysis | ~~re-map at finalization~~ **LANDED**: the finalizer re-maps the FULL stored span set (ClickHouse now keeps resource attrs) and refreshes runs/edges before announcing completion |
| no contract/requirement conventions | requirement divergence invisible; the crew's own RECOVERY of an injected rewrite was invisible too | ~~convention layer~~ **LANDED (lane, not adoption)**: `agent_detective.contract_params` opener-span attribute (JSON object) declares the input-side contract out-of-band → deterministic `contract_violations` even over prose/code inputs; SDK: `span.contract(file_type="pdf")`. The trace still has to SHOW the changed value in an output payload — and the vanilla crew doesn't stamp the attribute, so the injected cell stays honest `unverified` until the pipeline adopts the one attribute |
| ingest accepts OTLP/HTTP JSON only | standard protobuf exporters can't ship spans | ~~accept protobuf~~ **LANDED**: `application/x-protobuf` decoded on `/v1/traces` (ids re-hexed so both wire formats land on identical uuid5 rows) |
| form rubric quoted task scaffold as "the requirement" | requirement_provenance class on foreign traces | provenance gate: quote must trace to initial input |

## Non-requirements (deliberately)

- The detective does not need the harness to be honest: claims are typed as
  claims (judged channel, certainty < 1.0) until a deterministic check
  corroborates them.
- No live access to the agent system — everything above is trace content.
