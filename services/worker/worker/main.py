"""Worker entrypoint (build spec section 4).

A long-running process with three Redis Streams consumer groups run
concurrently under asyncio:

- ``tier1`` over ``ad.graphs.completed`` — cheap always-on detection;
- ``tier2`` over ``ad.graphs.tier2`` — full scoring + blame + incidents;
- ``alerters`` over ``ad.incidents.created`` — Slack/webhook notifications.

Plus a periodic dead-letter reaper that moves poison messages to ``*.dlq``.

Every external system sits behind a thin async protocol injected via
``Dependencies``; production builds the real client-backed implementations in
``build_dependencies``. All client constructors are lazy (no network I/O), so
importing this module has no side effects — tests inject in-memory fakes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .alerter import Alerter, HttpxWebhookClient, WebhookClient, run_alerter
from .config import Settings
from .judge_client import JudgeClient, OpenAIJudgeClient
from .repository import PgRepo, Repo
from .store import MinioObjectStore, ObjectStore
from .streams import (
    RedisStreams,
    StreamConsumer,
    StreamPublisher,
    reap_dead_letters,
)
from .tier1 import Tier1Processor, run_tier1
from .tier2 import Tier2Processor, run_tier2
from .types import (
    GROUP_ALERTERS,
    GROUP_TIER1,
    GROUP_TIER2,
    STREAM_GRAPHS_COMPLETED,
    STREAM_GRAPHS_TIER2,
    STREAM_INCIDENTS_CREATED,
)

logger = logging.getLogger(__name__)

_REAP_TARGETS = (
    (STREAM_GRAPHS_COMPLETED, GROUP_TIER1),
    (STREAM_GRAPHS_TIER2, GROUP_TIER2),
    (STREAM_INCIDENTS_CREATED, GROUP_ALERTERS),
)


@dataclass
class Dependencies:
    """Everything the worker needs from the outside world; one bag, all seams."""

    repo: Repo
    store: ObjectStore
    consumer: StreamConsumer
    publisher: StreamPublisher
    judge: JudgeClient
    webhook: WebhookClient


def build_dependencies(settings: Settings) -> Dependencies:
    """Construct the real client-backed implementations from settings.

    Client constructors are lazy (no network I/O), so importing this module and
    building dependencies is safe without live infrastructure.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    streams = RedisStreams.from_url(settings.redis_url)
    return Dependencies(
        repo=PgRepo(create_async_engine(settings.database_url)),
        store=MinioObjectStore.from_settings(settings),
        consumer=streams,
        publisher=streams,
        judge=OpenAIJudgeClient(settings),
        webhook=HttpxWebhookClient(),
    )


async def _reaper_loop(
    consumer: StreamConsumer,
    publisher: StreamPublisher,
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_s: float = 30.0,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        for stream, group in _REAP_TARGETS:
            try:
                await reap_dead_letters(
                    consumer,
                    publisher,
                    stream,
                    group,
                    f"{settings.consumer_name}-reaper",
                    settings.max_deliveries,
                    settings.reaper_idle_ms,
                )
            except Exception:
                logger.exception("reaper failed for %s/%s", stream, group)


async def _supervise(name: str, factory: "Callable[[], Awaitable[None]]", stop: asyncio.Event) -> None:
    """Run a consumer loop, restarting it if it crashes.

    A consumer that raises must not silently disappear (which would stall the
    stream forever); log the failure loudly and restart after a short backoff
    until shutdown is requested.
    """
    while not stop.is_set():
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s crashed; restarting in 2s", name)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass


async def run(settings: Settings, deps: Dependencies) -> None:
    """Run all consumers concurrently until a shutdown signal arrives."""
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            # add_signal_handler is unavailable on some platforms / threads.
            pass

    tier1 = Tier1Processor(deps.repo, deps.store, deps.publisher, deps.judge, settings)
    tier2 = Tier2Processor(deps.repo, deps.store, deps.publisher, deps.judge, settings)
    alerter = Alerter(deps.repo, deps.webhook, settings)

    tasks = [
        asyncio.create_task(
            _supervise("tier1", lambda: run_tier1(deps.consumer, tier1, settings, stop=stop), stop)
        ),
        asyncio.create_task(
            _supervise("tier2", lambda: run_tier2(deps.consumer, tier2, settings, stop=stop), stop)
        ),
        asyncio.create_task(
            _supervise("alerter", lambda: run_alerter(deps.consumer, alerter, settings, stop=stop), stop)
        ),
        asyncio.create_task(_reaper_loop(deps.consumer, deps.publisher, settings, stop)),
    ]
    try:
        await stop.wait()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            deps.repo.close(), deps.publisher.close(), return_exceptions=True
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    deps = build_dependencies(settings)
    asyncio.run(run(settings, deps))


if __name__ == "__main__":
    main()
