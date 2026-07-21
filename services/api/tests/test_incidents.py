import pytest

pytestmark = pytest.mark.anyio


async def test_list_incidents(client, ids):
    response = await client.get("/incidents")
    assert response.status_code == 200
    body = response.json()
    assert len(body["incidents"]) == 1
    incident = body["incidents"][0]
    assert incident["id"] == 1
    assert incident["graph_id"] == str(ids.GRAPH_ID)
    assert incident["incident_key"] == "degraded_quality"
    assert incident["trigger"] == "degraded_quality"
    assert incident["status"] == "open"
    assert incident["created_at"] is not None
    assert incident["updated_at"] is not None
    assert incident["latest_report"] == {
        "report_type": "cut_point",
        "culprit_run_ids": [str(ids.RUN_A)],
        "confidence": pytest.approx(0.87),
        "downstream_cost_usd": pytest.approx(0.31),
    }


async def test_list_incidents_without_report(client, repo):
    repo.reports = []
    response = await client.get("/incidents")
    assert response.status_code == 200
    assert response.json()["incidents"][0]["latest_report"] is None


async def test_incident_detail(client, ids):
    response = await client.get("/incidents/1")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1
    assert body["graph_id"] == str(ids.GRAPH_ID)
    assert body["incident_key"] == "degraded_quality"
    assert body["status"] == "open"

    report = body["latest_report"]
    assert report["id"] == 10
    assert report["incident_id"] == 1
    assert report["graph_id"] == str(ids.GRAPH_ID)
    assert report["version"] == 2
    assert report["is_latest"] is True
    assert report["report_type"] == "cut_point"
    assert report["culprit_run_ids"] == [str(ids.RUN_A)]
    assert report["propagation_path"] == [str(ids.RUN_A), str(ids.RUN_B), str(ids.RUN_C)]
    assert report["confidence"] == pytest.approx(0.87)
    assert report["downstream_cost_usd"] == pytest.approx(0.31)
    assert report["unscored_run_ids"] == []
    assert report["evidence"]["judge_reasoning"] == "fabricated prices"
    assert report["evidence"]["drops"] == [{"run_id": str(ids.RUN_B), "from": 0.9, "to": 0.4}]


async def test_incident_detail_404(client):
    response = await client.get("/incidents/999")
    assert response.status_code == 404


async def test_patch_incident_status(client):
    response = await client.patch("/incidents/1", json={"status": "acknowledged"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["updated_at"] > body["created_at"]
    # Detail shape is preserved on PATCH (latest report included).
    assert body["latest_report"]["report_type"] == "cut_point"

    follow_up = await client.get("/incidents/1")
    assert follow_up.json()["status"] == "acknowledged"

    resolved = await client.patch("/incidents/1", json={"status": "resolved"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    reopened = await client.patch("/incidents/1", json={"status": "open"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"


async def test_patch_incident_invalid_status_422(client):
    response = await client.patch("/incidents/1", json={"status": "closed"})
    assert response.status_code == 422


async def test_patch_incident_404(client):
    response = await client.patch("/incidents/999", json={"status": "resolved"})
    assert response.status_code == 404
