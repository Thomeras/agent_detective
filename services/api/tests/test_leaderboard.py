import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.anyio


async def test_leaderboard_math(client, repo, run_factory):
    repo.runs = [
        # scraper-agent: 3 runs, 1 failed, scores 0.5 / 0.7 / None (NULL excluded from avg)
        run_factory(uuid.uuid4(), agent_name="scraper-agent", status="ok", quality_score=0.5, cost_usd=Decimal("0.10")),
        run_factory(uuid.uuid4(), agent_name="scraper-agent", status="failed", quality_score=0.7, cost_usd=Decimal("0.20")),
        run_factory(uuid.uuid4(), agent_name="scraper-agent", status="ok", quality_score=None, unscored_reason="no_judge", cost_usd=None),
        # translator-agent: 1 run, healthy
        run_factory(uuid.uuid4(), agent_name="translator-agent", status="ok", quality_score=0.9, cost_usd=Decimal("0.05")),
    ]

    response = await client.get("/agents/leaderboard")
    assert response.status_code == 200
    agents = {a["agent_name"]: a for a in response.json()["agents"]}
    assert set(agents) == {"scraper-agent", "translator-agent"}

    scraper = agents["scraper-agent"]
    assert scraper["run_count"] == 3
    assert scraper["total_cost_usd"] == pytest.approx(0.30)
    assert scraper["failure_rate"] == pytest.approx(1 / 3)
    assert scraper["avg_quality_score"] == pytest.approx(0.6)  # (0.5 + 0.7) / 2, NULL excluded

    translator = agents["translator-agent"]
    assert translator["run_count"] == 1
    assert translator["failure_rate"] == pytest.approx(0.0)
    assert translator["avg_quality_score"] == pytest.approx(0.9)


async def test_leaderboard_ordered_by_cost_desc(client, repo, run_factory):
    repo.runs = [
        run_factory(uuid.uuid4(), agent_name="cheap-agent", cost_usd=Decimal("0.01")),
        run_factory(uuid.uuid4(), agent_name="pricey-agent", cost_usd=Decimal("2.00")),
    ]
    response = await client.get("/agents/leaderboard")
    names = [a["agent_name"] for a in response.json()["agents"]]
    assert names == ["pricey-agent", "cheap-agent"]


async def test_leaderboard_all_unscored(client, repo, run_factory):
    repo.runs = [run_factory(uuid.uuid4(), agent_name="unscored-agent", quality_score=None)]
    response = await client.get("/agents/leaderboard")
    agent = response.json()["agents"][0]
    assert agent["avg_quality_score"] is None
    assert agent["failure_rate"] == pytest.approx(0.0)


async def test_leaderboard_empty(client, repo):
    repo.runs = []
    response = await client.get("/agents/leaderboard")
    assert response.status_code == 200
    assert response.json() == {"agents": []}
