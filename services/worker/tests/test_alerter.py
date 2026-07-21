"""Alerter: alert only on is_new; webhook payload; console fallback."""

import asyncio

from worker.alerter import Alerter, build_slack_payload
from worker.types import AlertContext

from conftest import FakeRepo, FakeWebhook, make_settings, uid


def _seed_incident(repo: FakeRepo) -> int:
    repo.incidents[(uid(1), "degraded_quality")] = {
        "id": 7,
        "graph_id": uid(1),
        "incident_key": "degraded_quality",
        "trigger": "degraded_quality",
        "status": "open",
    }
    repo.blame_reports.append(
        {
            "id": 3,
            "incident_id": 7,
            "graph_id": uid(1),
            "version": 1,
            "is_latest": True,
            "report_type": "cut_point",
            "culprit_run_ids": [uid(2)],
            "propagation_path": [uid(2), uid(5)],
            "confidence": 0.82,
            "downstream_cost_usd": 0.1234,
            "unscored_run_ids": [],
            "evidence": {},
        }
    )
    return 7


def test_alerts_only_when_is_new_and_posts_webhook():
    repo = FakeRepo()
    _seed_incident(repo)
    webhook = FakeWebhook()
    settings = make_settings(slack_webhook_url="https://hooks.example/xyz")
    alerter = Alerter(repo, webhook, settings)

    asyncio.run(alerter.process({"incident_id": 7, "graph_id": str(uid(1)), "is_new": True}))
    assert len(webhook.posts) == 1
    url, payload = webhook.posts[0]
    assert url == "https://hooks.example/xyz"
    assert payload["incident_id"] == 7
    assert payload["trigger"] == "degraded_quality"
    assert payload["culprit_run_id"] == str(uid(2))
    assert payload["confidence"] == 0.82


def test_does_not_alert_when_not_new():
    repo = FakeRepo()
    _seed_incident(repo)
    webhook = FakeWebhook()
    alerter = Alerter(repo, webhook, make_settings(slack_webhook_url="https://hooks.example/xyz"))
    asyncio.run(alerter.process({"incident_id": 7, "graph_id": str(uid(1)), "is_new": False}))
    assert webhook.posts == []


def test_console_fallback_when_no_webhook_url():
    repo = FakeRepo()
    _seed_incident(repo)
    webhook = FakeWebhook()
    alerter = Alerter(repo, webhook, make_settings(slack_webhook_url=None))
    # No webhook configured -> logs to console, never posts.
    asyncio.run(alerter.process({"incident_id": 7, "graph_id": str(uid(1)), "is_new": True}))
    assert webhook.posts == []


def test_build_slack_payload_contains_ui_link():
    ctx = AlertContext(
        incident_id=7,
        graph_id=uid(1),
        trigger="degraded_quality",
        report_type="cut_point",
        culprit_run_ids=[uid(2)],
        confidence=0.82,
        downstream_cost_usd=0.5,
    )
    payload = build_slack_payload(ctx, "http://localhost:5173")
    assert payload["ui_link"] == f"http://localhost:5173/graphs/{uid(1)}"
    assert "incident #7" in payload["text"]
