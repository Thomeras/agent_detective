"""Alerter: Slack/webhook notifications (build spec section 4.3).

Consumes ``ad.incidents.created`` (group ``alerters``). It alerts **only** when
``is_new`` is true (a redelivered or re-scored incident does not re-notify),
loading the incident's trigger, culprit, confidence and downstream cost from
Postgres to render the payload. With ``SLACK_WEBHOOK_URL`` set it POSTs a Slack
message; without it, it logs the payload to the console.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .config import Settings
from .repository import Repo
from .streams import StreamConsumer, reclaim_pending_messages
from .types import GROUP_ALERTERS, STREAM_INCIDENTS_CREATED, AlertContext

logger = logging.getLogger(__name__)


class WebhookClient(Protocol):
    """Async seam for posting a JSON webhook; faked in tests."""

    async def post(self, url: str, payload: dict[str, Any]) -> None: ...


class HttpxWebhookClient:
    """httpx-backed webhook sender; the client is built lazily (no network I/O
    at construction)."""

    def __init__(self) -> None:
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def post(self, url: str, payload: dict[str, Any]) -> None:
        response = await self._http().post(url, json=payload)
        response.raise_for_status()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_slack_payload(ctx: AlertContext, ui_base_url: str) -> dict[str, Any]:
    """Render a Slack ``chat.postMessage``-style webhook payload."""
    culprit = ctx.culprit_run_ids[0] if ctx.culprit_run_ids else None
    ui_link = f"{ui_base_url.rstrip('/')}/graphs/{ctx.graph_id}"
    confidence = f"{ctx.confidence:.2f}" if ctx.confidence is not None else "n/a"
    cost = (
        f"${ctx.downstream_cost_usd:.4f}"
        if ctx.downstream_cost_usd is not None
        else "n/a"
    )
    lines = [
        f"*Agent Detective incident #{ctx.incident_id}* ({ctx.trigger})",
        f"Graph: `{ctx.graph_id}`",
        f"Blame: {ctx.report_type or 'unclassified'}",
        f"Suspected culprit: `{culprit}`" if culprit else "Suspected culprit: n/a",
        f"Confidence: {confidence}  |  Downstream cost: {cost}",
        f"<{ui_link}|Open in Agent Detective>",
    ]
    return {
        "text": f"Agent Detective incident #{ctx.incident_id} ({ctx.trigger})",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
        ],
        "incident_id": ctx.incident_id,
        "graph_id": str(ctx.graph_id),
        "trigger": ctx.trigger,
        "culprit_run_id": str(culprit) if culprit else None,
        "confidence": ctx.confidence,
        "downstream_cost_usd": ctx.downstream_cost_usd,
        "ui_link": ui_link,
    }


class Alerter:
    def __init__(
        self,
        repo: Repo,
        webhook: WebhookClient,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._webhook = webhook
        self._settings = settings

    async def process(self, data: dict[str, Any]) -> None:
        if not data.get("is_new"):
            logger.debug("alerter: incident %s not new; skipping", data.get("incident_id"))
            return
        incident_id = data.get("incident_id")
        if incident_id is None:
            return
        ctx = await self._repo.load_alert_context(int(incident_id))
        if ctx is None:
            logger.warning("alerter: incident %s not found", incident_id)
            return
        payload = build_slack_payload(ctx, self._settings.ui_base_url)
        if self._settings.slack_webhook_url:
            await self._webhook.post(self._settings.slack_webhook_url, payload)
            logger.info("alerter: posted incident #%s to Slack", ctx.incident_id)
        else:
            logger.warning("ALERT (no SLACK_WEBHOOK_URL): %s", payload["text"])


async def run_alerter(
    consumer: StreamConsumer,
    alerter: Alerter,
    settings: Settings,
    *,
    stop: "object | None" = None,
) -> None:
    """Consumer loop for ``ad.incidents.created`` (group ``alerters``)."""
    await consumer.ensure_group(STREAM_INCIDENTS_CREATED, GROUP_ALERTERS)
    while stop is None or not stop.is_set():
        # Reclaim orphaned pending entries (worker killed before XACK) so a
        # committed incident still gets its notification; alerting only fires on
        # is_new, so replaying a reclaimed message does not re-notify.
        reclaimed = await reclaim_pending_messages(
            consumer,
            STREAM_INCIDENTS_CREATED,
            GROUP_ALERTERS,
            settings.consumer_name,
            settings.reaper_idle_ms,
            settings.max_deliveries,
        )
        messages = await consumer.read(
            STREAM_INCIDENTS_CREATED,
            GROUP_ALERTERS,
            settings.consumer_name,
            settings.stream_batch_size,
            settings.stream_block_ms,
        )
        for message in reclaimed + messages:
            try:
                await alerter.process(message.data)
            except Exception:
                logger.exception("alerter: message %s failed", message.id)
                continue
            await consumer.ack(STREAM_INCIDENTS_CREATED, GROUP_ALERTERS, message.id)
