"""GET /agents/{agent_name}/versions/compare: tier1-based canary comparison."""

import uuid

import pytest

pytestmark = pytest.mark.anyio

G_BASE_1 = uuid.UUID("51111111-1111-1111-1111-111111111111")
G_BASE_2 = uuid.UUID("52222222-2222-2222-2222-222222222222")
G_CAND = uuid.UUID("53333333-3333-3333-3333-333333333333")


@pytest.fixture
def compare_repo(repo, run_factory, verdict_factory, incident_factory):
    repo.runs = [
        # base 1.0.0: two graphs, three runs
        run_factory(uuid.uuid4(), graph_id=G_BASE_1, agent_name="scraper-agent", agent_version="1.0.0", quality_score=0.8),
        run_factory(uuid.uuid4(), graph_id=G_BASE_1, agent_name="scraper-agent", agent_version="1.0.0", quality_score=0.6),
        run_factory(uuid.uuid4(), graph_id=G_BASE_2, agent_name="scraper-agent", agent_version="1.0.0", quality_score=None),
        # candidate 2.0.0: one graph
        run_factory(uuid.uuid4(), graph_id=G_CAND, agent_name="scraper-agent", agent_version="2.0.0", quality_score=0.3),
        # another agent's run in the same graph must not leak into scraper stats
        run_factory(uuid.uuid4(), graph_id=G_CAND, agent_name="translator-agent", agent_version="1.0.0", quality_score=0.1),
    ]
    repo.verdicts = {
        v["graph_id"]: v
        for v in [
            verdict_factory(G_BASE_1, terminal_judge_verdict="ok", flagged=False),
            verdict_factory(G_BASE_2, terminal_judge_verdict="bad", flagged=True),
            verdict_factory(G_CAND, terminal_judge_verdict="bad", flagged=True),
        ]
    }
    repo.incidents = {
        i["id"]: i
        for i in [incident_factory(incident_id=1, graph_id=G_CAND), incident_factory(incident_id=2, graph_id=G_CAND)]
    }
    return repo


async def test_versions_compare_happy_path(client, compare_repo):
    response = await client.get(
        "/agents/scraper-agent/versions/compare", params={"base": "1.0.0", "candidate": "2.0.0"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent_name"] == "scraper-agent"

    base = body["base"]
    assert base["agent_version"] == "1.0.0"
    assert base["graphs"] == 2
    assert base["runs"] == 3
    assert base["avg_quality"] == pytest.approx(0.7)  # NULL score excluded
    assert base["flag_rate"] == pytest.approx(0.5)
    assert base["terminal_bad_rate"] == pytest.approx(0.5)
    assert base["incidents"] == 0

    candidate = body["candidate"]
    assert candidate["agent_version"] == "2.0.0"
    assert candidate["graphs"] == 1
    assert candidate["runs"] == 1
    assert candidate["avg_quality"] == pytest.approx(0.3)
    assert candidate["flag_rate"] == pytest.approx(1.0)
    assert candidate["terminal_bad_rate"] == pytest.approx(1.0)
    assert candidate["incidents"] == 2


async def test_versions_compare_unknown_version_yields_empty_stats(client, compare_repo):
    response = await client.get(
        "/agents/scraper-agent/versions/compare", params={"base": "1.0.0", "candidate": "9.9.9"}
    )
    candidate = response.json()["candidate"]
    # No runs -> zero counts, and rates are null (nothing measured), never 0.0.
    assert candidate == {
        "agent_version": "9.9.9",
        "graphs": 0,
        "runs": 0,
        "avg_quality": None,
        "flag_rate": None,
        "terminal_bad_rate": None,
        "incidents": 0,
    }


async def test_versions_compare_no_verdicts_gives_null_rates(client, compare_repo):
    compare_repo.verdicts = {}
    body = (
        await client.get(
            "/agents/scraper-agent/versions/compare", params={"base": "1.0.0", "candidate": "2.0.0"}
        )
    ).json()
    assert body["base"]["flag_rate"] is None
    assert body["base"]["terminal_bad_rate"] is None
    assert body["base"]["runs"] == 3  # run/graph counts are still real observations


async def test_versions_compare_requires_params(client, compare_repo):
    response = await client.get("/agents/scraper-agent/versions/compare", params={"base": "1.0.0"})
    assert response.status_code == 422
