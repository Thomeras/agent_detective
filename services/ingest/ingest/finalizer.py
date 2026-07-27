"""Graph finalizer (build spec section 6.2).

An active graph is finalized when either:

- **root ended**: a run with no incoming edge has an end timestamp, or
- **quiescence**: no new spans arrived for ``GRAPH_QUIESCENCE_SECONDS``.

Last-seen-at-arrival is tracked in memory (``touch`` on every POST) because
quiescence is about *ingest* activity, not span timestamps, which may be
backdated. The DB fallback (max run start/end, then graph created_at) covers
process restarts: after a restart an untouched graph still ages out.

On finalize the graph row gets ``status='finalized'``, ``finalized_at``,
recomputed ``run_count`` and ``total_cost_usd``, and exactly one message is
published to ``ad.graphs.completed`` (spec 4.1)::

    {"schema_version": 1, "graph_id": "<uuid str>",
     "finalized_at": "<iso8601>", "run_count": N}

The repository's status guard makes a second finalize return None, so a
graph is never announced twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable
from uuid import UUID

from .pipeline import TraceRemapper
from .repository import Repo
from .stream import StreamPublisher
from .types import STREAM_GRAPHS_COMPLETED, FinalizeResult

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Finalizer:
    def __init__(
        self,
        repo: Repo,
        publisher: StreamPublisher,
        quiescence_seconds: float,
        *,
        clock: Callable[[], datetime] = _utcnow,
        stream: str = STREAM_GRAPHS_COMPLETED,
        remapper: "TraceRemapper | None" = None,
    ) -> None:
        self._repo = repo
        self._publisher = publisher
        self._quiescence = timedelta(seconds=quiescence_seconds)
        self._clock = clock
        self._stream = stream
        self._remapper = remapper
        # graph_id -> last time a POST delivered spans for it (arrival time).
        self._last_seen: dict[UUID, datetime] = {}

    def touch(self, graph_ids: Iterable[UUID], at: datetime | None = None) -> None:
        """Record ingest activity for graphs (called on every POST /v1/traces)."""
        now = at or self._clock()
        for graph_id in graph_ids:
            self._last_seen[graph_id] = now

    async def scan_once(self, now: datetime | None = None) -> list[FinalizeResult]:
        """Finalize every active graph that quiesced or whose root run ended."""
        now = now or self._clock()
        finalized: list[FinalizeResult] = []
        for activity in await self._repo.list_active_graph_activity():
            last_seen = (
                self._last_seen.get(activity.graph_id)
                or activity.last_activity
                or activity.created_at
            )
            quiesced = last_seen is not None and now - last_seen >= self._quiescence
            if not (quiesced or activity.root_ended):
                continue
            if self._remapper is not None:
                # Cross-batch structure (edges, late roots, trailing identity
                # spans) is only derivable over the FULL span set — re-map
                # before the run_count/total_cost freeze and the completed
                # announcement, so tier1 loads the finished topology.
                try:
                    await self._remapper.remap(activity.graph_id)
                except Exception:
                    logger.exception(
                        "re-map failed for graph %s; finalizing with per-batch mapping",
                        activity.graph_id,
                    )
            result = await self._repo.finalize_graph(activity.graph_id, now)
            if result is None:
                # Already finalized elsewhere; drop our stale activity marker.
                self._last_seen.pop(activity.graph_id, None)
                continue
            self._last_seen.pop(activity.graph_id, None)
            if result.run_count == 0:
                # Graph rows are only ever created for batches that carried
                # runs, so zero runs at finalize means the re-map re-homed
                # them all (e.g. a correlation header arrived in a later
                # batch). Finalize the empty shell silently — announcing it
                # would trigger an analysis of nothing.
                finalized.append(result)
                continue
            await self._publisher.xadd_json(
                self._stream,
                {
                    "schema_version": 1,
                    "graph_id": str(result.graph_id),
                    "finalized_at": result.finalized_at.isoformat(),
                    "run_count": result.run_count,
                },
            )
            finalized.append(result)
        return finalized

    async def run_forever(self, check_seconds: float) -> None:
        """Background loop: scan every ``check_seconds``; log and continue on errors."""
        while True:
            await asyncio.sleep(check_seconds)
            try:
                finalized = await self.scan_once()
                for result in finalized:
                    logger.info(
                        "finalized graph %s (run_count=%d)", result.graph_id, result.run_count
                    )
            except Exception:
                logger.exception("finalizer scan failed")
