#!/usr/bin/env bash
# Run every package/service unit suite. Each suite runs from its own directory:
# service test modules import shared helpers as `from conftest import ...`, which
# only resolves unambiguously when pytest's rootdir is the package dir (all
# service source roots share one venv via the workspace .pth, so a bare
# `conftest` collides across services when collected from the repo root).
#
# This mirrors the CI `unit` job. The end-to-end suite (tests/e2e) needs a
# running stack and is run separately; see .github/workflows/ci.yml.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

uv sync --all-packages --all-groups --frozen

run() { # <dir> <package> [extra pytest args...]
  local dir="$1" pkg="$2"; shift 2
  echo "== ${pkg} =="
  ( cd "$dir" && uv run --package "$pkg" pytest tests "$@" )
}

run packages/blame_engine blame-engine --cov=blame_engine --cov-fail-under=90
run packages/otel_mapper  otel-mapper
run packages/detective_sdk detective-sdk
run packages/detective_ci  detective-ci
run packages/detective_cli agent-detective
run services/ingest       ingest
run services/api          api
run services/worker       agent-detective-worker
# Foreign traces: replay only, no model and no network (see corpus/README.md).
run corpus                agent-detective-corpus

echo "All unit suites passed."
