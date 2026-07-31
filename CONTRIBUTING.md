# Contributing

Thanks for looking. This is a young project with strong opinions about what it
is allowed to claim, so this document spends most of its length on those rather
than on formatting.

## Licensing, before anything else

The repository is **Business Source License 1.1** (converting to Apache-2.0 on
2030-07-22), with two deliberate exceptions: `packages/otel_mapper/` and
`packages/detective_sdk/` are **Apache-2.0**.

That split is not a compromise, it is the boundary of the product. Both
exempted packages run inside somebody else's system — the mapper is the
adoption surface, and the SDK is imported into the user's agent and ships in
their product, which the BSL grant would class as a Production Use. Nothing
that merely *emits* a trace may require a licence, including in things that
compete with the rest. What stays under BSL is what *reads* the trace and
returns a verdict.

By opening a pull request you agree your contribution ships under the licence
of the files it touches.

If you are contributing something substantial, say so in the issue first. It is
easier to agree on where a thing belongs before it is written.

## What is most useful

- **A trace that produces a wrong verdict.** The most valuable bug report in
  this project is a trace file plus what the report said and what it should have
  said. Every fix in 0.2.0 came from one real run. Redact payloads freely — the
  graph shape and the attributes are usually what matters.
- **Instrumentation for a framework that has none.** See
  [docs/instrumentation.md](docs/instrumentation.md).
- **A deterministic check.** The channel that needs no model is the one that
  scales; see below for the shape.
- **Verdict semantics** (what counts as an origin, how confidence is computed)
  are worth discussing in an issue first. They are the product.

## Setup

```bash
uv sync --all-packages --all-groups
./scripts/test.sh                 # every unit suite, exactly as CI runs them
```

Python 3.12+. `scripts/test.sh` mirrors the CI `unit` job. Each suite runs from
**its own directory** rather than the repo root, and that is not cosmetic:
service test modules import shared helpers as `from conftest import ...`, and
several services have same-named test modules, so collecting from the root makes
`conftest` ambiguous. Run a single suite the same way:

```bash
cd packages/blame_engine && uv run --package blame-engine pytest tests
```

`blame_engine` carries a **90% coverage floor** in CI. The end-to-end suite
(`tests/e2e`) needs the compose stack up and runs as a separate CI job.

## Layout

| path | what it is | ships as |
|---|---|---|
| `packages/otel_mapper/` | OTLP spans → run/edge candidates | `otel-mapper` (Apache-2.0) |
| `packages/blame_engine/` | pure, I/O-free blame analysis | `blame-engine` |
| `packages/detective_sdk/` | instrumentation helpers, zero deps | `detective-sdk` (Apache-2.0) |
| `packages/detective_cli/` | the local-mode CLI | `agent-detective` |
| `packages/detective_ci/` | golden replay + CI gate | `detective-ci` |
| `services/worker/` | tier1/tier2 processors, scoring, signals | `agent-detective-worker` |
| `services/ingest/`, `services/api/` | deployed stack only | not published |
| `db/alembic/` | schema migrations | — |
| `web/` | React UI (self-host demo; no auth yet) | — |

`detective_sdk` has **zero runtime dependencies** on purpose — instrumentation
runs inside the agent's own process and must not drag a judge, a database or the
OpenTelemetry SDK in with it. Please keep it that way.

## House rules

These are the ones that will get a PR sent back, and each exists because the
opposite shipped once and was wrong.

**Absent evidence never becomes a number.** Not `0.0`, not `$0`, not "fine". An
unmeasured node is UNKNOWN, an uninstrumented cost is `None`, an empty payload is
unscored. Scoring `""` as `0.0` is the strongest possible claim from the weakest
possible evidence, and it made orchestrator wrapper spans the culprit of every
run they appeared in. If an absence *is* the defect, prove it on the
deterministic channel — see `empty_output` in `services/worker/worker/behavioral.py`
for the pattern: a positive discriminator, then a named signal.

**Say when you cannot tell.** A claim the trace does not support is worse than
no claim. `detective doctor` exists entirely to make that distinction visible,
and `"supported": null` is a legitimate answer there.

**Decision code emits typed records; prose is rendered once.** Classification
builds `NoteRecord`/`CandidacyRecord` objects with a slug and a data dict; the
sentences live in `narrative.py` template tables. Tests assert on slugs and
payloads, never on wording — otherwise every rewording is a breaking change, and
a template can drift away from the data it describes without anything noticing.
`test_narrative.py` fails if a template has no scenario that emits it, or a slug
has no template.

**Deterministic and judged are separate channels.** A fail-severity signal rides
out as evidence and localises blame on its own; it does not floor the judged
score. Keep the provenance with the check.

**Comments explain why, especially the failure that motivated the code.** The
existing comments are long where the reasoning is non-obvious and absent where
it is not. Match that. A comment restating the line below it is noise; one
recording the bug that the line prevents is the most valuable thing in the file.

## Adding a deterministic signal

1. A **pure function** in `services/worker/worker/behavioral.py`,
   `checks_content.py` or `checks_security.py`, returning
   `signal(NAME, severity, code, **params)`. No I/O, no identity — the caller
   stamps `run_id`/`agent`. Malformed input yields `[]`, never an exception.
2. A **template** for the `code` in `services/worker/worker/narrative.py`
   (`_SIGNAL_TEMPLATES`): one renderer for the human `detail`, one for the
   machine-checkable `basis`.
3. Wire it into `score_node` in `scoring.py`.
4. Tests for the check itself, and for the boundary where it should *not* fire.
   `severity="fail"` localises blame, so it needs a discriminator that is a fact
   about the run, not a plausible inference.

## Schema changes

Migrations live in `db/alembic/versions/`, numbered and chained via
`down_revision`. A new column that carries span data needs the whole path or it
only works in local CLI mode: `otel_mapper` → `RunRecord` →
`detective_cli/bundle.py` (local) **and** `services/ingest` + `worker/pg.py`
(deployed).

## Commits and releases

Commit subjects are `type: what changed` (`fix:`, `feat:`, `docs:`, `chore:`) in
the imperative. The body is for *why* — see `git log` for the register.

Releases are cut from `main` with a tag and a `CHANGELOG.md` entry. Distributions
are versioned independently; publish **leaves first**
(`otel-mapper`, `detective-sdk`, `blame-engine` → `agent-detective-worker` →
`agent-detective`), or the CLI briefly requires a dependency version that does
not exist yet.

## Contact

Anything that does not fit an issue — collaboration, commercial licensing, or a
question about whether your use is covered by the grant: **tomje11@seznam.cz**.
