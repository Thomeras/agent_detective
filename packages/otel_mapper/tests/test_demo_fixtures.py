"""The checked-in demo-pipeline fixtures exercise the full graph model.

The synthetic demo (``demo/synthetic_pipeline``) is the product's showcase, so
its captured OTLP payloads (repo-root ``testdata/``) must exercise every edge
type the mapper can derive: SPAWN fan-out, the TOOL_DELEGATION chain, an
A2A_MESSAGE edge, and a retry loop — a cycle among run nodes (compliance spawns
a scraper retry run whose A2A answer flows back into compliance).
"""

import json
from pathlib import Path

import pytest

from otel_mapper import EdgeType, flatten_export_request, map_spans

REPO_TESTDATA = Path(__file__).resolve().parents[3] / "testdata"

FIXTURES = ["demo_pipeline_happy.json", "demo_pipeline_faulted.json"]


def _mapped(name: str):
    payload = json.loads((REPO_TESTDATA / name).read_text(encoding="utf-8"))
    return map_spans(flatten_export_request(payload), a2a_detection=True)


def _named_edges(result) -> set[tuple[str, str, EdgeType]]:
    names = {r.run_key: r.agent_name for r in result.runs}
    return {(names[e.from_run_key], names[e.to_run_key], e.type) for e in result.edges}


def _has_cycle(result) -> bool:
    adjacency: dict[str, list[str]] = {}
    for e in result.edges:
        adjacency.setdefault(e.from_run_key, []).append(e.to_run_key)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {r.run_key: WHITE for r in result.runs}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in adjacency.get(node, []):
            if color[nxt] == GRAY:
                return True
            if color[nxt] == WHITE and visit(nxt):
                return True
        color[node] = BLACK
        return False

    return any(visit(n) for n in color if color[n] == WHITE)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_demo_fixture_retry_is_a_separate_run_node(fixture: str) -> None:
    result = _mapped(fixture)
    # Six runs: five agents plus the scraper retry. The mapper keys runs on
    # the opening AGENT span, so the second scraper-agent AGENT span is a
    # distinct node, not collapsed by agent name.
    assert len(result.runs) == 6
    scraper_runs = [r for r in result.runs if r.agent_name == "scraper-agent"]
    assert len(scraper_runs) == 2
    assert scraper_runs[0].run_key != scraper_runs[1].run_key


@pytest.mark.parametrize("fixture", FIXTURES)
def test_demo_fixture_exercises_all_edge_types(fixture: str) -> None:
    result = _mapped(fixture)
    named = _named_edges(result)

    # SPAWN fan-out from the orchestrator is preserved.
    for child in ("scraper-agent", "translator-agent", "compliance-agent", "publisher-agent"):
        assert ("orchestrator", child, EdgeType.SPAWN) in named

    # TOOL_DELEGATION chain (target -> caller, direction of data flow).
    assert ("scraper-agent", "compliance-agent", EdgeType.TOOL_DELEGATION) in named
    assert ("translator-agent", "compliance-agent", EdgeType.TOOL_DELEGATION) in named
    assert ("compliance-agent", "publisher-agent", EdgeType.TOOL_DELEGATION) in named

    # The A2A re-scrape answer flows from the retry run back into compliance.
    assert ("scraper-agent", "compliance-agent", EdgeType.A2A_MESSAGE) in named

    # Compliance spawns the retry run (second scraper-agent node).
    assert ("compliance-agent", "scraper-agent", EdgeType.SPAWN) in named


@pytest.mark.parametrize("fixture", FIXTURES)
def test_demo_fixture_contains_retry_loop_cycle(fixture: str) -> None:
    result = _mapped(fixture)
    assert _has_cycle(result)

    # The cycle is exactly compliance -> retry (SPAWN) -> compliance (A2A).
    runs = {r.run_key: r for r in result.runs}
    retry_key = max(
        (r for r in result.runs if r.agent_name == "scraper-agent"),
        key=lambda r: r.start_time,
    ).run_key
    compliance_key = next(
        r.run_key for r in result.runs if r.agent_name == "compliance-agent"
    )
    pairs = {(e.from_run_key, e.to_run_key, e.type) for e in result.edges}
    assert (compliance_key, retry_key, EdgeType.SPAWN) in pairs
    assert (retry_key, compliance_key, EdgeType.A2A_MESSAGE) in pairs
    assert runs[retry_key].agent_name == "scraper-agent"


@pytest.mark.parametrize("fixture", FIXTURES)
def test_demo_fixture_has_no_cycle_without_a2a_detection(fixture: str) -> None:
    # The cycle depends on the feature-flagged A2A rule: with detection off the
    # demo graph must stay acyclic (SPAWN/TOOL_DELEGATION only).
    payload = json.loads((REPO_TESTDATA / fixture).read_text(encoding="utf-8"))
    result = map_spans(flatten_export_request(payload), a2a_detection=False)
    assert not _has_cycle(result)
    assert all(e.type is not EdgeType.A2A_MESSAGE for e in result.edges)
