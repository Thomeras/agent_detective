import uuid

import pytest

pytestmark = pytest.mark.anyio


async def test_payloads_served_inline(client, store, ids):
    response = await client.get(f"/graphs/{ids.GRAPH_ID}/payloads/{ids.RUN_A}")
    assert response.status_code == 200
    body = response.json()
    assert body["graph_id"] == str(ids.GRAPH_ID)
    assert body["run_id"] == str(ids.RUN_A)
    assert body["input"] == {"source": "inline", "content": '{"task": "scrape"}', "bytes": 18}
    assert body["output"] == {"source": "inline", "content": '{"items": 3}', "bytes": 13}
    # Nothing was fetched from the object store.
    assert store.requested == []


async def test_payloads_overflow_ref_routing(client, repo, store, ids, run_factory):
    run_id = uuid.uuid4()
    repo.runs.append(
        run_factory(
            run_id,
            input_inline=None,
            input_overflow_ref=f"payloads/{ids.GRAPH_ID}/{run_id}/input",
            input_bytes=100,
            output_inline=None,
            output_overflow_ref=f"payloads/{ids.GRAPH_ID}/{run_id}/output",
            output_bytes=200,
        )
    )
    store.objects[f"payloads/{ids.GRAPH_ID}/{run_id}/input"] = "x" * 100
    store.objects[f"payloads/{ids.GRAPH_ID}/{run_id}/output"] = "y" * 200

    response = await client.get(f"/graphs/{ids.GRAPH_ID}/payloads/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["input"] == {"source": "overflow", "content": "x" * 100, "bytes": 100}
    assert body["output"] == {"source": "overflow", "content": "y" * 200, "bytes": 200}
    assert store.requested == [
        f"payloads/{ids.GRAPH_ID}/{run_id}/input",
        f"payloads/{ids.GRAPH_ID}/{run_id}/output",
    ]


async def test_payloads_missing_run_404(client, ids):
    response = await client.get(f"/graphs/{ids.GRAPH_ID}/payloads/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_payloads_run_from_other_graph_404(client, ids):
    # The run exists but belongs to a different graph.
    response = await client.get(f"/graphs/{ids.OTHER_GRAPH_ID}/payloads/{ids.RUN_A}")
    assert response.status_code == 404
