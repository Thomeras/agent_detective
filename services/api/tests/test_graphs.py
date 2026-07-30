import uuid

import pytest

pytestmark = pytest.mark.anyio


async def test_list_graphs(client, ids):
    response = await client.get("/graphs")
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["graphs"]) == 1
    graph = body["graphs"][0]
    assert graph["id"] == str(ids.GRAPH_ID)
    assert graph["name"] == "demo-pipeline"
    assert graph["graph_type"] == "synthetic_pipeline"
    assert graph["status"] == "finalized"
    assert graph["run_count"] == 3
    assert graph["total_cost_usd"] == pytest.approx(0.42)
    assert graph["started_at"].startswith("2026-07-20T12:00:00")
    assert graph["ended_at"] is not None


async def test_list_graphs_pagination(client, repo, ids, graph_factory):
    repo.graphs[ids.OTHER_GRAPH_ID] = graph_factory(ids.OTHER_GRAPH_ID, name="other")

    first_page = await client.get("/graphs", params={"limit": 1, "offset": 0})
    second_page = await client.get("/graphs", params={"limit": 1, "offset": 1})
    assert first_page.status_code == 200
    assert len(first_page.json()["graphs"]) == 1
    assert len(second_page.json()["graphs"]) == 1
    first_id = first_page.json()["graphs"][0]["id"]
    second_id = second_page.json()["graphs"][0]["id"]
    assert first_id != second_id

    empty_page = await client.get("/graphs", params={"limit": 10, "offset": 5})
    assert empty_page.json()["graphs"] == []


async def test_list_graphs_rejects_bad_limit(client):
    response = await client.get("/graphs", params={"limit": 0})
    assert response.status_code == 422


async def test_graph_detail_cytoscape_shape(client, ids):
    response = await client.get(f"/graphs/{ids.GRAPH_ID}")
    assert response.status_code == 200
    body = response.json()

    assert body["id"] == str(ids.GRAPH_ID)
    assert body["total_cost_usd"] == pytest.approx(0.42)
    assert body["run_count"] == 3

    nodes = {node["data"]["id"]: node["data"] for node in body["nodes"]}
    assert set(nodes) == {str(ids.RUN_A), str(ids.RUN_B), str(ids.RUN_C)}
    node_a = nodes[str(ids.RUN_A)]
    assert node_a["agent_name"] == "scraper-agent"
    assert node_a["status"] == "ok"
    assert node_a["quality_score"] == pytest.approx(0.9)
    assert node_a["score_components"] == {"schema": 1.0, "judge": 0.85, "heuristics": 0.8}
    assert node_a["unscored_reason"] is None
    assert node_a["input_flawed"] is False
    assert node_a["cost_usd"] == pytest.approx(0.10)
    assert node_a["tokens_in"] == 120
    assert node_a["tokens_out"] == 45
    assert node_a["started_at"] is not None
    assert node_a["ended_at"] is not None

    edges = body["edges"]
    assert len(edges) == 2
    edge = edges[0]["data"]
    assert edge == {
        "id": "1",
        "source": str(ids.RUN_A),
        "target": str(ids.RUN_B),
        "type": "SPAWN",
        "detection_method": "span_parent",
    }


async def test_graph_detail_404(client):
    response = await client.get(f"/graphs/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_graph_detail_marks_never_analyzed_run(client, repo, run_factory, ids):
    # NULL score + NULL reason can only mean no analysis ever covered the run.
    repo.runs = [
        run_factory(ids.RUN_A, quality_score=None, score_components=None, unscored_reason=None),
    ]
    response = await client.get(f"/graphs/{ids.GRAPH_ID}")
    node = response.json()["nodes"][0]["data"]
    assert node["quality_score"] is None
    assert node["unscored_reason"] == "not_analyzed"


async def test_graph_detail_keeps_engine_unscored_reason(client, repo, run_factory, ids):
    repo.runs = [
        run_factory(ids.RUN_A, quality_score=None, score_components=None, unscored_reason="payload_missing"),
    ]
    response = await client.get(f"/graphs/{ids.GRAPH_ID}")
    node = response.json()["nodes"][0]["data"]
    assert node["unscored_reason"] == "payload_missing"
