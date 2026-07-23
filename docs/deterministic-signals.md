# Deterministic signals — design (v1, 2026-07-23)

## Concept

A **deterministic signal** is a named, reproducible check result with provenance
`deterministic` (not LLM). Signals follow three rules:

1. **Named** — every check emits a stable signal name (`artifact_integrity_fail`,
   `contract_violation`, `numeric_invariant_breach`, `loop_fingerprint`, …), so
   evidence, alerting and dashboards key off identifiers, not prose.
2. **Provenance-first** — a signal always carries `{name, run_id, severity,
   detail, basis}`; `basis` states the observation it rests on (magic bytes,
   param diff, span fingerprint). Signals are never glued into LLM judge notes.
3. **Deterministic beats judge** — a `fail`-severity signal overrides LLM
   opinion: it caps the node score (like `contract_violation` → 0.15) and, on
   the deliverable, overrides the terminal verdict (a corrupt artifact is
   ground-truth `bad` at score 0.0, no LLM call needed).

Existing precedents already following this shape: `contract_violation`
(scoring.py override → 0.15), `unverifiable_artifact` (opaque-ref regex → judge
cap 0.6), tier1 flags (`failed_runs`, `cost_overrun`, `loop_anomaly`,
`schema_violation`, `degenerate_output`), `score_overrides` (terminal ground
truth vs verifier PASS). This design generalizes them into one evidence stream.

## Signal shape (Evidence JSONB, additive — no migration)

```json
{
  "name": "artifact_integrity_fail",
  "run_id": "<uuid>",
  "severity": "fail" | "warn",
  "detail": "declared .docx but content is plain text",
  "basis": "magic bytes: first 4 bytes 'Výh…' != PK",
  "provenance": "deterministic"
}
```

- `blame_engine.Evidence.deterministic_signals: list[dict]` — node-level
  signals assembled by the engine from `NodeScore.deterministic_signals`;
  graph-level (tier1) signals appended by the worker post-serialize.
- `blame_engine.NodeScore.deterministic_signals: tuple[dict, ...]` — populated
  by worker scoring.

## Universality

Agent Detective is agent-agnostic. Everything below is defined as a **platform
convention** (attribute names + payload markers documented in
`docs/instrumentation.md`) plus a reusable, dependency-free helper package
**`packages/detective_sdk`** that ANY instrumentation can adopt — a LangChain
exporter, an OpenAI-SDK wrapper, a custom loop. The worker-side checks parse
the convention, never a specific agent. `generative_simon` is merely the
*reference integration* (and the live end-to-end test harness): its exporter
consumes `detective_sdk` the same way any third-party agent would.

## A1 — Artifact integrity (this iteration)

The worker cannot open files (they live on the instrumented host). The
**instrumentation** already opens artifacts to embed `artifact_text`; it also
computes an integrity record (via `detective_sdk.artifact_meta(path)`) and
ships it **out-of-band as a span attribute**:

```
agent_detective.artifact_meta =
  [{"path": "output/report.md", "size": 12345, "sha256": "ab12…",
    "declared_ext": "md", "detected_kind": "text", "parse_ok": true,
    "nonempty": true}]
```

(compact JSON array string, one entry per artifact path in that span's output,
incl. the deliverable fallback for terminal spans; ingest lands it verbatim in
`agent_runs.artifact_meta`).

**Out-of-band is load-bearing.** The first design shipped the meta as a payload
text block (`[artifact_meta <path>]: {...}`); adversarial review proved that
in-band signaling is forgeable — document content can *quote* a failing block
(forcing a false deterministic `bad` on a healthy run) or collide with the
exporter's double-attach guard (suppressing the real block and masking a
genuinely corrupt artifact). Span attributes cannot be injected by document
content, so the attribute is the only authoritative source — the worker never
parses integrity meta out of payload text.

Detection table (instrumentation side, magic bytes): `PK\x03\x04` → `zip`
(docx/xlsx/pptx family), `%PDF` → `pdf`, decodable UTF-8 with printable ratio →
`text`, zero bytes → `empty`, else `binary`. `parse_ok`: docx = zipfile opens
AND `word/document.xml` present; pdf = header+`%%EOF`; md/txt/html = utf-8
decode.

Worker checks (`worker/signals.py`, pure functions):

| check              | fail condition                                     | signal                    |
|--------------------|----------------------------------------------------|---------------------------|
| magic vs extension | `declared_ext` maps to kind ≠ `detected_kind`      | `artifact_integrity_fail` |
| parseability       | `parse_ok == false`                                | `artifact_integrity_fail` |
| non-empty + size   | `nonempty == false` or `size < min_artifact_bytes` | `artifact_integrity_fail` |
| meta missing       | file-ref payload without meta (opaque)             | *(existing `unverifiable_artifact` path — no new signal)* |

Wiring:
- **tier1**: check the deliverable run's `artifact_meta` attribute next to
  `is_degenerate_output`. On `fail`: flag `artifact_integrity` + terminal
  verdict is **deterministically `bad` (score 0.0, checkable=True)** with the
  signal detail as reasoning — the LLM judge is skipped (cost 0, and it cannot
  be fooled by claims). Min size lives in Settings (`min_artifact_bytes`).
- **tier2 scoring**: same checks on each run's own `artifact_meta` → signal +
  `flags += ("artifact_integrity_fail",)` + `components["artifact_integrity"]=0.0`
  + score cap 0.10 (mirrors the contract override; a corrupt artifact is a
  harder fact than a rewritten param).
- **engine**: assembles node-level signals into
  `Evidence.deterministic_signals`; no classification change needed — the score
  cap localizes blame through the ordinary cut-point path, and the deterministic
  bad terminal drives the existing terminal_bad machinery.

## B1 — Versioning (this iteration)

Per-run identity: **agent_version** (existing column, never sent),
**model_name**, **prompt_hash** (new nullable Text columns on `agent_runs`,
migration 0006; mirrored in api models + serializer whitelist + UI NodePanel).

Universal attribute contract (any instrumentation; helpers in `detective_sdk`):
- `gen_ai.agent.version` = the agent codebase version — `detective_sdk`
  provides `git_version(repo_dir)` (short sha, `-dirty` suffix, cached).
- `gen_ai.request.model` = the model identifier the run used.
- `agent_detective.prompt_hash` = 12 hex chars of sha256 over the files that
  define the agent's prompts (`detective_sdk.content_hash(paths)`).

Reference integration (generative_simon): version = its git sha; model = its
`_MODEL` constant (lazy import, no cycles); prompt_hash over `agent/prompts.py`
+ `design/component-catalog.json` (covers the dynamic `build_system_prompt`).

Mapper: `model_name` ← `gen_ai.request.model`, `prompt_hash` ←
`agent_detective.prompt_hash`, span-attrs-first-then-resource (same rule as
agent_version). Known limitation: `agent_runs` upsert is `ON CONFLICT DO
NOTHING`, so already-ingested runs stay null (forward-only).

Migration 0006 also fixes a latent drift found during recon:
`ck_tier1_verdicts_verdict` still allows only `('ok','bad','error')` while tier1
emits `not_checkable` since the P0 phantom fix — the constraint gains it.

“Proč to včera fungovalo?” = diff of (agent_version, prompt_hash, model_name)
between two graphs — data is now recorded; a dedicated diff view is roadmap.

## Is the detective deterministic?

The product sells auditability, so the detective's own reproducibility is a
number to KNOW, not to assume. Tier0/deterministic checks are stable by
construction; the tier1/tier2 **LLM judge is in the verdict path**, so a fixed
trace can still yield different verdicts between runs — a node scoring near the
0.50 blame threshold can flip an edge and move the verdict (e.g.
`degraded_recovered` ↔ `shipped`).

Two tools:

- **`JUDGE_SEED`** (worker env, `Settings.judge_seed`, default unset): when
  set, the judge client sends `"seed": <value>` in every `/chat/completions`
  request, alongside the existing `temperature: 0`. Honest caveat:
  temperature=0 + seed still does **not** guarantee bitwise-identical LLM
  output on most backends (batching, GPU nondeterminism, provider-side
  changes); it narrows variance, it does not eliminate it.
- **`scripts/determinism_probe.py`**: re-analyzes one already-ingested graph N
  times (`POST /graphs/{id}/analyze`, waits for each new versioned blame
  report) and reports the verdict distribution, culprit stability,
  confidence / attribution_confidence spread, per-node score mean/stddev/
  min/max, and flags nodes within 0.10 of the 0.50 threshold as flip risks.
  Exit code 0 only when verdicts are 100% stable (1 = variance, 2 =
  inconclusive), so it can gate CI against a seeded stack:

  ```
  scripts/determinism_probe.py --graph-id <uuid> --rounds 10 [--json]
  ```

  The pure summary math (`summarize_rounds`) is unit-tested without the stack
  in `tests/test_determinism_probe.py`.

## Out of scope here → docs/roadmap.md

A2 numeric/structural invariants, A3 behavioral trace signals beyond the
existing loop/cost checks, A4 security scans, B2–B7 (policy gates, circuit
breaker, canary, golden snapshots, audit trail, feedback loop) — mapped onto
this signal framework with priorities in the roadmap document.
