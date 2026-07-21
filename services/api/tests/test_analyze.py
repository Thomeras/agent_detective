import uuid
from datetime import datetime

import pytest

pytestmark = pytest.mark.anyio


async def test_analyze_publishes_exact_stream_message(client, publisher, ids):
    response = await client.post(f"/graphs/{ids.GRAPH_ID}/analyze")
    assert response.status_code == 200
    body = response.json()

    assert len(publisher.messages) == 1
    message = publisher.messages[0]

    dedup_key = body["dedup_key"]
    assert dedup_key.startswith(f"{ids.GRAPH_ID}:manual:")
    uuid.UUID(dedup_key.rsplit(":", 1)[1])  # trailing component is a uuid4

    assert message == {
        "schema_version": "1",
        "graph_id": str(ids.GRAPH_ID),
        "trigger": "manual",
        "dedup_key": dedup_key,
        "tier1_verdict_ref": str(ids.GRAPH_ID),
        "requested_at": message["requested_at"],
    }
    # requested_at is a parseable ISO-8601 timestamp.
    assert datetime.fromisoformat(message["requested_at"]).tzinfo is not None
    assert body["stream_id"] == "0-1"


async def test_analyze_without_tier1_verdict_sends_null_ref(client, repo, publisher, ids):
    repo.tier1_graph_ids = set()
    response = await client.post(f"/graphs/{ids.GRAPH_ID}/analyze")
    assert response.status_code == 200
    assert publisher.messages[0]["tier1_verdict_ref"] == ""


async def test_analyze_each_call_new_dedup_key(client, publisher, ids):
    first = await client.post(f"/graphs/{ids.GRAPH_ID}/analyze")
    second = await client.post(f"/graphs/{ids.GRAPH_ID}/analyze")
    assert first.json()["dedup_key"] != second.json()["dedup_key"]
    assert len(publisher.messages) == 2


async def test_analyze_graph_missing_404(client, publisher):
    response = await client.post(f"/graphs/{uuid.uuid4()}/analyze")
    assert response.status_code == 404
    assert publisher.messages == []
