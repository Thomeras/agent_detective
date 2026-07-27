"""Shared test harness: in-memory fakes for every external seam.

The committed suite is fully network-free. Fakes implement the same async
protocols as the real clients (Repo, SpanSink, StreamPublisher, ObjectStore)
and emulate the database's idempotency semantics (ON CONFLICT DO NOTHING,
NULL-ignoring LEAST/GREATEST, count-based run_count) so tests assert on the
state the real Postgres would hold.

OTLP fixtures are reused from packages/otel_mapper/testdata (not duplicated).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from ingest.config import Settings
from ingest.main import Dependencies, create_app
from ingest.types import EdgeRow, FinalizeResult, GraphActivity, IngestBatch, RunRow, SpanRow

TESTDATA_DIR = (
    Path(__file__).resolve().parents[3] / "packages" / "otel_mapper" / "testdata"
)


def load_fixture(name: str) -> Any:
    return json.loads((TESTDATA_DIR / name).read_text(encoding="utf-8"))


def _min_none(a: datetime | None, b: datetime | None) -> datetime | None:
    values = [v for v in (a, b) if v is not None]
    return min(values) if values else None


def _max_none(a: datetime | None, b: datetime | None) -> datetime | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


class FakeRepo:
    """In-memory Repo with Postgres-equivalent upsert semantics."""

    def __init__(self) -> None:
        self.graphs: dict[UUID, dict[str, Any]] = {}
        self.runs: dict[UUID, RunRow] = {}
        self.edges: list[EdgeRow] = []
        self._edge_keys: set[tuple[Any, ...]] = set()
        self.fail_ping = False

    async def upsert_batch(self, batch: IngestBatch, *, refresh_runs: bool = False) -> None:
        for graph in batch.graphs:
            existing = self.graphs.get(graph.graph_id)
            if existing is None:
                self.graphs[graph.graph_id] = {
                    "graph_id": graph.graph_id,
                    "graph_type": graph.graph_type,
                    "status": "active",
                    "started_at": graph.started_at,
                    "ended_at": graph.ended_at,
                    "finalized_at": None,
                    "run_count": 0,
                    "total_cost_usd": None,
                    "created_at": graph.started_at,
                }
            else:
                # ON CONFLICT DO UPDATE with NULL-ignoring LEAST/GREATEST.
                existing["started_at"] = _min_none(existing["started_at"], graph.started_at)
                existing["ended_at"] = _max_none(existing["ended_at"], graph.ended_at)
                # coalesce(existing, excluded): keep the first-known cohort key.
                existing["graph_type"] = existing["graph_type"] or graph.graph_type
        for run in batch.runs:
            if refresh_runs:
                self.runs[run.run_id] = run  # ON CONFLICT DO UPDATE (re-map)
            else:
                self.runs.setdefault(run.run_id, run)  # ON CONFLICT DO NOTHING
        for edge in batch.edges:
            key = (edge.graph_id, edge.from_run_id, edge.to_run_id, edge.type)
            if key not in self._edge_keys:
                self._edge_keys.add(key)
                self.edges.append(edge)
        for graph_id in batch.graph_ids:
            self.graphs[graph_id]["run_count"] = self._run_count(graph_id)

    def _run_count(self, graph_id: UUID) -> int:
        return sum(1 for r in self.runs.values() if r.graph_id == graph_id)

    async def trace_ids_for_graph(self, graph_id: UUID) -> list[str]:
        return sorted(
            {r.trace_id for r in self.runs.values() if r.graph_id == graph_id and r.trace_id}
        )

    async def list_active_graph_activity(self) -> list[GraphActivity]:
        out: list[GraphActivity] = []
        for graph in self.graphs.values():
            if graph["status"] != "active":
                continue
            graph_runs = [r for r in self.runs.values() if r.graph_id == graph["graph_id"]]
            times = [
                t for r in graph_runs for t in (r.started_at, r.ended_at) if t is not None
            ]
            root_ended = any(
                r.ended_at is not None
                and not any(
                    e.graph_id == r.graph_id and e.to_run_id == r.run_id for e in self.edges
                )
                for r in graph_runs
            )
            out.append(
                GraphActivity(
                    graph_id=graph["graph_id"],
                    last_activity=max(times) if times else None,
                    created_at=graph["created_at"],
                    root_ended=root_ended,
                )
            )
        return out

    async def finalize_graph(self, graph_id: UUID, finalized_at: datetime) -> FinalizeResult | None:
        graph = self.graphs.get(graph_id)
        if graph is None or graph["status"] != "active":
            return None
        graph["status"] = "finalized"
        graph["finalized_at"] = finalized_at
        graph["run_count"] = self._run_count(graph_id)
        graph["total_cost_usd"] = sum(
            r.cost_usd or 0.0 for r in self.runs.values() if r.graph_id == graph_id
        )
        return FinalizeResult(
            graph_id=graph_id, finalized_at=finalized_at, run_count=graph["run_count"]
        )

    async def ping(self) -> None:
        if self.fail_ping:
            raise ConnectionError("postgres unreachable")

    async def close(self) -> None:
        pass


class FakeSpanSink:
    def __init__(self) -> None:
        self.rows: list[SpanRow] = []
        self.fail_ping = False

    async def insert_spans(self, rows: list[SpanRow]) -> None:
        self.rows.extend(rows)

    async def select_spans(self, trace_ids: list[str]) -> list[dict[str, Any]]:
        # Same row->span reconstruction as the real sink, so tests exercise
        # the exact storage round-trip (epoch sentinel, stringified kinds).
        from ingest.spans import _dedupe_latest, mappable_span

        selected = [r for r in self.rows if r.trace_id in set(trace_ids)]
        return [mappable_span(row) for row in _dedupe_latest(selected)]

    async def ping(self) -> None:
        if self.fail_ping:
            raise ConnectionError("clickhouse unreachable")

    async def close(self) -> None:
        pass


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.fail_ping = False

    async def xadd_json(self, stream: str, message: dict[str, Any]) -> str:
        self.messages.append((stream, message))
        return f"0-{len(self.messages)}"

    async def ping(self) -> None:
        if self.fail_ping:
            raise ConnectionError("redis unreachable")

    async def close(self) -> None:
        pass


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()

    async def put(self, bucket: str, key: str, data: bytes) -> str:
        self.objects[(bucket, key)] = data
        return f"s3://{bucket}/{key}"

    async def ensure_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)


class Harness:
    """App wired to fakes; requests run on a per-call event loop."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.repo = FakeRepo()
        self.sink = FakeSpanSink()
        self.publisher = FakePublisher()
        self.store = FakeObjectStore()
        deps = Dependencies(
            repo=self.repo,
            span_sink=self.sink,
            publisher=self.publisher,
            store=self.store,
        )
        self.app = create_app(self.settings, deps)
        self.finalizer = self.app.state.finalizer

    async def post_traces(self, payload: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            return await client.post("/v1/traces", json=payload)

    async def post_raw(
        self, content: bytes, content_type: str = "application/json"
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            return await client.post(
                "/v1/traces", content=content, headers={"content-type": content_type}
            )

    async def health(self) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            return await client.get("/health")


@pytest.fixture()
def harness() -> Harness:
    return Harness()
