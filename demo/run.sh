#!/usr/bin/env bash
# Run the synthetic five-agent pipeline against the ingest endpoint.
#
# Endpoints are configurable via environment so the same script works against
# docker-compose or a local checkout:
#   INGEST_URL     default http://localhost:8001   (empty => dry run, no POST)
#   LLM_BASE_URL   default http://localhost:8080/v1 (the mock LLM)
#
# The fault armed by ./demo/inject_fault.sh (or SCRAPER_HALLUCINATE=1 in the
# environment) makes the scraper fabricate prices. The marker is one-shot.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
PIPELINE_DIR="${DEMO_DIR}/synthetic_pipeline"
MARKER="${DEMO_DIR}/.fault_armed"

export INGEST_URL="${INGEST_URL:-http://localhost:8001}"
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:8080/v1}"
export SCRAPER_HALLUCINATE="${SCRAPER_HALLUCINATE:-0}"

if [ -f "${MARKER}" ]; then
  export SCRAPER_HALLUCINATE=1
  rm -f "${MARKER}"
  echo "Fault marker consumed: running with SCRAPER_HALLUCINATE=1"
fi

case "${SCRAPER_HALLUCINATE}" in
  1|true|TRUE|yes) MODE="FAULTED (scraper hallucinates prices)";;
  *)               MODE="clean";;
esac

echo "Agent Detective demo pipeline"
echo "  mode:        ${MODE}"
echo "  ingest:      ${INGEST_URL:-<dry run>}"
echo "  llm:         ${LLM_BASE_URL}"
echo

if command -v uv >/dev/null 2>&1; then
  ( cd "${PIPELINE_DIR}" && uv run --quiet python -m synthetic_pipeline )
else
  echo "uv not found; falling back to python (deps must be installed)"
  ( cd "${PIPELINE_DIR}" && python -m synthetic_pipeline )
fi

echo
echo "Done. The graph should appear in the API/UI shortly (after finalization)."
