#!/usr/bin/env bash
# Arm the flagship fault for the *next* ./demo/run.sh: the scraper agent will
# fabricate prices the source pages never listed, while every downstream agent
# processes them faithfully (silent hallucination). run.sh consumes and clears
# this one-shot marker, so a subsequent plain run is clean again.
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="${DEMO_DIR}/.fault_armed"

touch "${MARKER}"
echo "Fault armed: SCRAPER_HALLUCINATE=1 will apply to the next ./demo/run.sh"
echo "(marker: ${MARKER})"
