# Changelog

Notable changes per release. Versions follow [semver](https://semver.org),
with the beta caveat stated in the README: while the project is `0.x`, a MINOR
bump may change what a verdict is *called* on an unchanged trace. The exit-code
contract (`0` analysed and clean, `1` incident, `2` analysis could not run) is
stable and is the thing to gate CI on.

Distributions are versioned independently; a release lists the ones that moved.

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
