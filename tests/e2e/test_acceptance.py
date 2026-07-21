"""End-to-end acceptance test against a running compose stack (build spec M6).

This is the acceptance test for the whole MVP. It drives the real synthetic
pipeline through the live stack and asserts the observable outcome via the read
API:

- a clean run produces a finalized graph and NO incident;
- a faulted run (silent hallucination in the scraper) produces, within a
  bounded time, a ``degraded_quality`` incident whose latest blame report is a
  ``cut_point`` naming ``scraper-agent``, with the propagation path
  scraper-agent -> compliance-agent -> publisher-agent and confidence > 0.

The test runs the pipeline exactly like ``demo/run.sh`` (a subprocess via uv),
with a unique correlation id per run so it is safe to re-run against a
non-truncated stack. It is skipped automatically when the stack is not
reachable, so a plain ``pytest`` run does not require infrastructure.

Endpoints are configurable via environment:
    E2E_API_URL       default http://localhost:8000
    E2E_INGEST_URL    default http://localhost:8001
    E2E_LLM_BASE_URL  default http://localhost:8080/v1
    E2E_TIMEOUT_S     default 90
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

API_URL = os.environ.get("E2E_API_URL", "http://localhost:8000").rstrip("/")
INGEST_URL = os.environ.get("E2E_INGEST_URL", "http://localhost:8001").rstrip("/")
LLM_BASE_URL = os.environ.get("E2E_LLM_BASE_URL", "http://localhost:8080/v1")
TIMEOUT_S = float(os.environ.get("E2E_TIMEOUT_S", "90"))

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = REPO_ROOT / "demo" / "synthetic_pipeline"

# Matches ingest.types.graph_id_from_str (uuid5 over NAMESPACE_URL).
_NAMESPACE = uuid.NAMESPACE_URL


def graph_uuid(correlation_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, correlation_id))


def _get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0, None


def _stack_up() -> bool:
    status, _ = _get(f"{API_URL}/health")
    if status != 200:
        return False
    status, _ = _get(f"{INGEST_URL}/health")
    return status == 200


pytestmark = pytest.mark.skipif(
    not _stack_up(),
    reason=(
        "compose stack not reachable; start it with `docker compose up` "
        "(set E2E_API_URL / E2E_INGEST_URL to override)"
    ),
)


def run_pipeline(correlation_id: str, *, hallucinate: bool) -> str:
    """Run the synthetic pipeline against the live ingest; return graph UUID."""
    env = dict(os.environ)
    env.update(
        INGEST_URL=INGEST_URL,
        LLM_BASE_URL=LLM_BASE_URL,
        GRAPH_ID=correlation_id,
        SCRAPER_HALLUCINATE="1" if hallucinate else "0",
        # Non-deterministic ids so every run creates fresh runs/spans.
        DETERMINISTIC="0",
    )
    subprocess.run(
        ["uv", "run", "--quiet", "python", "-m", "synthetic_pipeline"],
        cwd=str(PIPELINE_DIR),
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    return graph_uuid(correlation_id)


def poll(predicate, timeout_s: float = TIMEOUT_S, interval_s: float = 2.0):
    """Poll predicate() until it returns a truthy value or the timeout."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    return last


def find_incident_for_graph(graph_id: str) -> dict | None:
    status, body = _get(f"{API_URL}/incidents")
    if status != 200 or not isinstance(body, dict):
        return None
    for incident in body.get("incidents", []):
        if incident.get("graph_id") == graph_id:
            return incident
    return None


def test_clean_run_has_graph_and_no_incident() -> None:
    correlation_id = f"e2e-happy-{uuid.uuid4()}"
    graph_id = run_pipeline(correlation_id, hallucinate=False)

    # The graph should appear (after finalization) via the API.
    graph = poll(lambda: (_get(f"{API_URL}/graphs/{graph_id}")[1] or None))
    assert graph is not None, f"graph {graph_id} never appeared in the API"

    # A clean run must not raise an incident. Give the pipeline a fair window
    # (tier1 must run and decide not to escalate) then assert none exists.
    time.sleep(min(20.0, TIMEOUT_S))
    assert find_incident_for_graph(graph_id) is None, "clean run produced an incident"


def test_faulted_run_produces_cut_point_incident_on_scraper() -> None:
    correlation_id = f"e2e-fault-{uuid.uuid4()}"
    graph_id = run_pipeline(correlation_id, hallucinate=True)

    incident = poll(lambda: find_incident_for_graph(graph_id))
    assert incident is not None, (
        f"no incident for faulted graph {graph_id} within {TIMEOUT_S}s"
    )
    assert incident["incident_key"] == "degraded_quality"

    report = incident.get("latest_report")
    assert report is not None, "incident has no blame report"
    assert report["report_type"] == "cut_point"
    assert report["confidence"] > 0

    # Full report detail (culprit names + path) from the incident endpoint.
    status, detail = _get(f"{API_URL}/incidents/{incident['id']}")
    assert status == 200 and isinstance(detail, dict)
    full = detail.get("latest_report") or report

    # Resolve culprit + path run ids to agent names via the graph payload.
    # Nodes are cytoscape-style: {"data": {"id": <run_id>, "agent_name": ...}}.
    _, graph = _get(f"{API_URL}/graphs/{graph_id}")
    assert isinstance(graph, dict)
    name_by_run = {
        node["data"].get("id"): node["data"].get("agent_name")
        for node in graph.get("nodes", [])
    }
    culprit_names = [name_by_run.get(rid) for rid in full.get("culprit_run_ids", [])]
    assert culprit_names == ["scraper-agent"], culprit_names

    path_names = [name_by_run.get(rid) for rid in full.get("propagation_path", [])]
    assert path_names == [
        "scraper-agent",
        "compliance-agent",
        "publisher-agent",
    ], path_names
