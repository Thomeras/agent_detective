"""Policy decisions (shadow observations), human feedback and breaker state."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


# --- GET /graphs/{id}/policy-decisions ---


async def test_policy_decisions_happy_path(client, repo, ids):
    repo.policy_decisions = [
        {
            "id": 2,
            "graph_id": ids.GRAPH_ID,
            "rule_name": "cost-ceiling",
            "decision": "would_warn",
            "detail": "cost 1.20 over 1.00",
            "mode": "shadow",
            "created_at": T0 + timedelta(minutes=2),
        },
        {
            "id": 1,
            "graph_id": ids.GRAPH_ID,
            "rule_name": "block-degenerate",
            "decision": "would_block",
            "detail": "flag degenerate_output present",
            "mode": "shadow",
            "created_at": T0 + timedelta(minutes=1),
        },
        {
            "id": 3,
            "graph_id": ids.OTHER_GRAPH_ID,
            "rule_name": "other-graph-rule",
            "decision": "would_warn",
            "detail": None,
            "mode": "shadow",
            "created_at": T0,
        },
    ]
    response = await client.get(f"/graphs/{ids.GRAPH_ID}/policy-decisions")
    assert response.status_code == 200
    decisions = response.json()["decisions"]
    assert [d["rule_name"] for d in decisions] == ["block-degenerate", "cost-ceiling"]
    first = decisions[0]
    assert first["decision"] == "would_block"  # shadow mode: "would have", never "did"
    assert first["mode"] == "shadow"
    assert first["detail"] == "flag degenerate_output present"
    assert first["created_at"].startswith("2026-07-20T12:01:00")


async def test_policy_decisions_empty_and_404(client, repo, ids):
    response = await client.get(f"/graphs/{ids.GRAPH_ID}/policy-decisions")
    assert response.status_code == 200
    assert response.json() == {"decisions": []}

    missing = await client.get(f"/graphs/{uuid.uuid4()}/policy-decisions")
    assert missing.status_code == 404


# --- POST /graphs/{id}/feedback ---


async def test_feedback_happy_path(client, repo, ids):
    response = await client.post(
        f"/graphs/{ids.GRAPH_ID}/feedback",
        json={"label": "bad", "culprit_run_id": str(ids.RUN_B), "note": "wrong language"},
    )
    assert response.status_code == 200
    assert response.json() == {"id": 1}
    stored = repo.labels[0]
    assert stored["graph_id"] == ids.GRAPH_ID
    assert stored["label"] == "bad"
    assert stored["culprit_run_id"] == ids.RUN_B
    assert stored["source"] == "human"
    assert stored["note"] == "wrong language"


async def test_feedback_minimal_body(client, repo, ids):
    response = await client.post(f"/graphs/{ids.GRAPH_ID}/feedback", json={"label": "ok"})
    assert response.status_code == 200
    stored = repo.labels[0]
    assert stored["culprit_run_id"] is None
    assert stored["note"] is None


async def test_feedback_rejects_bad_label(client, repo, ids):
    response = await client.post(f"/graphs/{ids.GRAPH_ID}/feedback", json={"label": "meh"})
    assert response.status_code == 400
    assert repo.labels == []


async def test_feedback_rejects_bad_culprit_uuid(client, ids):
    response = await client.post(
        f"/graphs/{ids.GRAPH_ID}/feedback", json={"label": "bad", "culprit_run_id": "not-a-uuid"}
    )
    assert response.status_code == 400


async def test_feedback_unknown_graph_404(client):
    response = await client.post(f"/graphs/{uuid.uuid4()}/feedback", json={"label": "ok"})
    assert response.status_code == 404


# --- GET /control/breakers ---


async def test_breakers_list(client, repo):
    repo.breakers = [
        {
            "id": 1,
            "scope_kind": "agent_name",
            "scope_value": "scraper-agent",
            "state": "open",
            "reason": "3 open incidents blame scraper-agent",
            "opened_at": T0,
            "updated_at": T0 + timedelta(minutes=5),
        },
        {
            "id": 2,
            "scope_kind": "agent_name",
            "scope_value": "publisher-agent",
            "state": "closed",
            "reason": None,
            "opened_at": None,
            "updated_at": T0,
        },
    ]
    response = await client.get("/control/breakers")
    assert response.status_code == 200
    breakers = response.json()["breakers"]
    # Ordered by scope_kind, scope_value.
    assert [b["scope_value"] for b in breakers] == ["publisher-agent", "scraper-agent"]
    open_breaker = breakers[1]
    assert open_breaker == {
        "scope_kind": "agent_name",
        "scope_value": "scraper-agent",
        "state": "open",
        "reason": "3 open incidents blame scraper-agent",
        "opened_at": T0.isoformat(),
        "updated_at": (T0 + timedelta(minutes=5)).isoformat(),
    }


async def test_breakers_empty(client, repo):
    response = await client.get("/control/breakers")
    assert response.status_code == 200
    assert response.json() == {"breakers": []}
