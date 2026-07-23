"""GET /graphs/{id}/version-diff: per-agent identity diff against a baseline graph."""

import uuid
from datetime import timedelta

import pytest

pytestmark = pytest.mark.anyio

CLEAN_OLD = uuid.UUID("41111111-1111-1111-1111-111111111111")
CLEAN_NEW = uuid.UUID("42222222-2222-2222-2222-222222222222")
INCIDENT_GRAPH = uuid.UUID("43333333-3333-3333-3333-333333333333")


@pytest.fixture
def diff_repo(repo, graph_factory, run_factory, ids):
    """Two clean finalized graphs (old and new) plus one with an incident."""
    t0 = repo.graphs[ids.GRAPH_ID]["started_at"]
    repo.graphs[CLEAN_OLD] = graph_factory(CLEAN_OLD, finalized_at=t0 + timedelta(hours=1))
    repo.graphs[CLEAN_NEW] = graph_factory(CLEAN_NEW, finalized_at=t0 + timedelta(hours=2))
    repo.graphs[INCIDENT_GRAPH] = graph_factory(INCIDENT_GRAPH, finalized_at=t0 + timedelta(hours=3))
    repo.incidents[99] = {
        "id": 99,
        "graph_id": INCIDENT_GRAPH,
        "incident_key": "k",
        "trigger": "t",
        "status": "open",
        "created_at": t0,
        "updated_at": t0,
    }
    # Baseline (CLEAN_NEW) ran scraper with a different prompt_hash and had no
    # publisher-agent at all.
    repo.runs.extend(
        [
            run_factory(uuid.uuid4(), graph_id=CLEAN_NEW, agent_name="scraper-agent", prompt_hash="oldhash00001"),
            run_factory(uuid.uuid4(), graph_id=CLEAN_NEW, agent_name="translator-agent"),
            run_factory(uuid.uuid4(), graph_id=CLEAN_OLD, agent_name="scraper-agent", prompt_hash="ancienthash1"),
        ]
    )
    return repo


async def test_last_clean_resolution_picks_most_recent_clean_graph(client, diff_repo, ids):
    # INCIDENT_GRAPH is newer but has an incidents row; CLEAN_NEW must win.
    response = await client.get(f"/graphs/{ids.GRAPH_ID}/version-diff")
    assert response.status_code == 200
    body = response.json()
    assert body["graph_id"] == str(ids.GRAPH_ID)
    assert body["against"] == str(CLEAN_NEW)
    assert body["against_mode"] == "last_clean"

    per_agent = {row["agent_name"]: row for row in body["per_agent"]}
    assert set(per_agent) == {"scraper-agent", "translator-agent", "publisher-agent"}

    scraper = per_agent["scraper-agent"]
    assert scraper["current"] == {
        "agent_version": "1.0.0",
        "model_name": "mock-model",
        "prompt_hash": "abc123abc123",
        "tool_schema_hash": "def456def456",
    }
    assert scraper["baseline"]["prompt_hash"] == "oldhash00001"
    assert scraper["changed"] == ["prompt_hash"]

    assert per_agent["translator-agent"]["changed"] == []

    # Agent absent from the baseline graph: baseline null, nothing to diff.
    publisher = per_agent["publisher-agent"]
    assert publisher["baseline"] is None
    assert publisher["changed"] == []


async def test_last_clean_excludes_self(client, repo, ids, graph_factory, run_factory):
    # The current graph is itself clean; last_clean must resolve to an OTHER graph.
    del repo.incidents[1]
    repo.graphs[CLEAN_OLD] = graph_factory(CLEAN_OLD)
    repo.runs.append(run_factory(uuid.uuid4(), graph_id=CLEAN_OLD, agent_name="scraper-agent"))
    body = (await client.get(f"/graphs/{ids.GRAPH_ID}/version-diff")).json()
    assert body["against"] == str(CLEAN_OLD)


async def test_no_clean_graph_yields_null_against(client, repo, ids):
    # Only the current graph exists (and it has an incident anyway).
    body = (await client.get(f"/graphs/{ids.GRAPH_ID}/version-diff")).json()
    assert body["against"] is None
    assert body["against_mode"] == "last_clean"
    assert all(row["baseline"] is None and row["changed"] == [] for row in body["per_agent"])


async def test_explicit_against(client, diff_repo, ids):
    response = await client.get(
        f"/graphs/{ids.GRAPH_ID}/version-diff", params={"against": str(CLEAN_OLD)}
    )
    body = response.json()
    assert body["against"] == str(CLEAN_OLD)
    assert body["against_mode"] == "explicit"
    per_agent = {row["agent_name"]: row for row in body["per_agent"]}
    assert per_agent["scraper-agent"]["baseline"]["prompt_hash"] == "ancienthash1"
    assert per_agent["scraper-agent"]["changed"] == ["prompt_hash"]
    # translator-agent never ran in CLEAN_OLD.
    assert per_agent["translator-agent"]["baseline"] is None


async def test_latest_run_by_started_at_wins(client, diff_repo, ids, run_factory):
    t0 = diff_repo.graphs[ids.GRAPH_ID]["started_at"]
    # A later scraper retry in the current graph with a bumped version.
    diff_repo.runs.append(
        run_factory(
            uuid.uuid4(),
            agent_name="scraper-agent",
            agent_version="1.1.0",
            started_at=t0 + timedelta(minutes=10),
        )
    )
    body = (await client.get(f"/graphs/{ids.GRAPH_ID}/version-diff")).json()
    scraper = next(row for row in body["per_agent"] if row["agent_name"] == "scraper-agent")
    assert scraper["current"]["agent_version"] == "1.1.0"
    assert set(scraper["changed"]) == {"agent_version", "prompt_hash"}


async def test_null_vs_value_counts_as_changed(client, diff_repo, ids):
    for run in diff_repo.runs:
        if run["graph_id"] == ids.GRAPH_ID and run["agent_name"] == "translator-agent":
            run["tool_schema_hash"] = None
    body = (await client.get(f"/graphs/{ids.GRAPH_ID}/version-diff")).json()
    translator = next(row for row in body["per_agent"] if row["agent_name"] == "translator-agent")
    assert translator["changed"] == ["tool_schema_hash"]


async def test_version_diff_bad_against_and_404(client, diff_repo, ids):
    bad = await client.get(f"/graphs/{ids.GRAPH_ID}/version-diff", params={"against": "nonsense"})
    assert bad.status_code == 400

    missing_baseline = await client.get(
        f"/graphs/{ids.GRAPH_ID}/version-diff", params={"against": str(uuid.uuid4())}
    )
    assert missing_baseline.status_code == 404

    missing_graph = await client.get(f"/graphs/{uuid.uuid4()}/version-diff")
    assert missing_graph.status_code == 404
