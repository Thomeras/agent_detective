"""Network-free test fixtures: in-memory fakes wired via dependency_overrides."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from api import deps
from api.config import Settings
from api.main import create_app

GRAPH_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_GRAPH_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
RUN_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RUN_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
RUN_C = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_run(run_id: uuid.UUID, graph_id: uuid.UUID = GRAPH_ID, **overrides: Any) -> dict[str, Any]:
    run = {
        "run_id": run_id,
        "graph_id": graph_id,
        "agent_name": "scraper-agent",
        "agent_version": "1.0.0",
        "parent_run_id": None,
        "trace_id": f"trace-{run_id.hex[:8]}",
        "status": "ok",
        "input_inline": '{"task": "scrape"}',
        "input_overflow_ref": None,
        "input_bytes": 18,
        "output_inline": '{"items": 3}',
        "output_overflow_ref": None,
        "output_bytes": 13,
        "input_summary": "scrape task",
        "output_summary": "3 items",
        "quality_score": 0.9,
        "score_components": {"schema": 1.0, "judge": 0.85, "heuristics": 0.8},
        "unscored_reason": None,
        "input_flawed": False,
        "cost_usd": Decimal("0.10"),
        "tokens_in": 120,
        "tokens_out": 45,
        "started_at": T0,
        "ended_at": T0 + timedelta(seconds=5),
    }
    run.update(overrides)
    return run


def make_graph(graph_id: uuid.UUID = GRAPH_ID, **overrides: Any) -> dict[str, Any]:
    graph = {
        "graph_id": graph_id,
        "name": "demo-pipeline",
        "graph_type": "synthetic_pipeline",
        "status": "finalized",
        "started_at": T0,
        "ended_at": T0 + timedelta(minutes=2),
        "finalized_at": T0 + timedelta(minutes=2, seconds=30),
        "total_cost_usd": Decimal("0.42"),
        "run_count": 3,
        "created_at": T0,
    }
    graph.update(overrides)
    return graph


def make_incident(incident_id: int = 1, graph_id: uuid.UUID = GRAPH_ID, **overrides: Any) -> dict[str, Any]:
    incident = {
        "id": incident_id,
        "graph_id": graph_id,
        "incident_key": "degraded_quality",
        "trigger": "degraded_quality",
        "status": "open",
        "created_at": T0,
        "updated_at": T0,
    }
    incident.update(overrides)
    return incident


def make_report(report_id: int = 10, incident_id: int = 1, **overrides: Any) -> dict[str, Any]:
    report = {
        "id": report_id,
        "incident_id": incident_id,
        "graph_id": GRAPH_ID,
        "version": 2,
        "is_latest": True,
        "report_type": "cut_point",
        "culprit_run_ids": [RUN_A],
        "propagation_path": [RUN_A, RUN_B, RUN_C],
        "confidence": 0.87,
        "downstream_cost_usd": Decimal("0.31"),
        "unscored_run_ids": [],
        "evidence": {"drops": [{"run_id": str(RUN_B), "from": 0.9, "to": 0.4}], "judge_reasoning": "fabricated prices"},
        "created_at": T0 + timedelta(minutes=3),
    }
    report.update(overrides)
    return report


class FakeRepository:
    """In-memory stand-in implementing the Repository protocol."""

    def __init__(
        self,
        graphs: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        incidents: list[dict[str, Any]] | None = None,
        reports: list[dict[str, Any]] | None = None,
        tier1_graph_ids: set[uuid.UUID] | None = None,
    ):
        self.graphs = {g["graph_id"]: g for g in graphs or []}
        self.runs = runs or []
        self.edges = edges or []
        self.incidents = {i["id"]: i for i in incidents or []}
        self.reports = reports or []
        self.tier1_graph_ids = tier1_graph_ids or set()
        self.now = T0 + timedelta(hours=1)

    async def list_graphs(self, limit: int, offset: int) -> list[dict[str, Any]]:
        ordered = sorted(self.graphs.values(), key=lambda g: (g["started_at"] is None, g["started_at"], g["graph_id"]))
        return ordered[offset : offset + limit]

    async def get_graph(self, graph_id: uuid.UUID) -> dict[str, Any] | None:
        return self.graphs.get(graph_id)

    async def list_runs(self, graph_id: uuid.UUID) -> list[dict[str, Any]]:
        return [r for r in self.runs if r["graph_id"] == graph_id]

    async def list_edges(self, graph_id: uuid.UUID) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["graph_id"] == graph_id]

    async def get_run(self, graph_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any] | None:
        for run in self.runs:
            if run["graph_id"] == graph_id and run["run_id"] == run_id:
                return run
        return None

    async def list_incidents(self, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = []
        for incident in self.incidents.values():
            latest = self._latest_report(incident["id"])
            row = dict(incident)
            row["report_id"] = latest["id"] if latest else None
            row["report_type"] = latest["report_type"] if latest else None
            row["culprit_run_ids"] = latest["culprit_run_ids"] if latest else None
            row["confidence"] = latest["confidence"] if latest else None
            row["downstream_cost_usd"] = latest["downstream_cost_usd"] if latest else None
            rows.append(row)
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return rows[offset : offset + limit]

    async def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        return self.incidents.get(incident_id)

    async def get_latest_report(self, incident_id: int) -> dict[str, Any] | None:
        return self._latest_report(incident_id)

    def _latest_report(self, incident_id: int) -> dict[str, Any] | None:
        candidates = [r for r in self.reports if r["incident_id"] == incident_id and r["is_latest"]]
        return max(candidates, key=lambda r: r["version"]) if candidates else None

    async def update_incident_status(self, incident_id: int, status: str) -> dict[str, Any] | None:
        incident = self.incidents.get(incident_id)
        if incident is None:
            return None
        incident["status"] = status
        incident["updated_at"] = self.now
        return incident

    async def leaderboard(self) -> list[dict[str, Any]]:
        by_agent: dict[str, list[dict[str, Any]]] = {}
        for run in self.runs:
            by_agent.setdefault(run["agent_name"], []).append(run)
        rows = []
        for agent_name, agent_runs in by_agent.items():
            scored = [r["quality_score"] for r in agent_runs if r["quality_score"] is not None]
            failed = sum(1 for r in agent_runs if r["status"] == "failed")
            rows.append(
                {
                    "agent_name": agent_name,
                    "total_cost_usd": sum((r["cost_usd"] or Decimal("0")) for r in agent_runs),
                    "run_count": len(agent_runs),
                    "failure_rate": failed / len(agent_runs),
                    "avg_quality_score": (sum(scored) / len(scored)) if scored else None,
                }
            )
        rows.sort(key=lambda r: (-r["total_cost_usd"], r["agent_name"]))
        return rows

    async def has_tier1_verdict(self, graph_id: uuid.UUID) -> bool:
        return graph_id in self.tier1_graph_ids


class FakePayloadStore:
    """In-memory MinIO stand-in keyed by object key."""

    def __init__(self, objects: dict[str, str] | None = None):
        self.objects = dict(objects or {})
        self.requested: list[str] = []

    async def get_text(self, object_key: str) -> str:
        self.requested.append(object_key)
        if object_key not in self.objects:
            raise KeyError(object_key)
        return self.objects[object_key]


class FakePublisher:
    """Captures XADD calls to ad.graphs.tier2."""

    def __init__(self):
        self.messages: list[dict[str, str]] = []

    async def publish_tier2(self, message: dict[str, str]) -> str:
        self.messages.append(message)
        return f"0-{len(self.messages)}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Shared fixtures are exposed as fixtures (not conftest imports) so the suite
# also collects under the root's --import-mode=importlib.
@pytest.fixture
def ids() -> SimpleNamespace:
    return SimpleNamespace(
        GRAPH_ID=GRAPH_ID,
        OTHER_GRAPH_ID=OTHER_GRAPH_ID,
        RUN_A=RUN_A,
        RUN_B=RUN_B,
        RUN_C=RUN_C,
    )


@pytest.fixture
def run_factory():
    return make_run


@pytest.fixture
def graph_factory():
    return make_graph


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository(
        graphs=[make_graph()],
        runs=[make_run(RUN_A), make_run(RUN_B, agent_name="translator-agent"), make_run(RUN_C, agent_name="publisher-agent")],
        edges=[
            {"id": 1, "graph_id": GRAPH_ID, "from_run_id": RUN_A, "to_run_id": RUN_B, "type": "SPAWN", "detection_method": "span_parent"},
            {"id": 2, "graph_id": GRAPH_ID, "from_run_id": RUN_B, "to_run_id": RUN_C, "type": "TOOL_DELEGATION", "detection_method": "tool_attr"},
        ],
        incidents=[make_incident()],
        reports=[make_report()],
        tier1_graph_ids={GRAPH_ID},
    )


@pytest.fixture
def store() -> FakePayloadStore:
    return FakePayloadStore()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def app(repo: FakeRepository, store: FakePayloadStore, publisher: FakePublisher):
    application = create_app(Settings())
    application.dependency_overrides[deps.get_repository] = lambda: repo
    application.dependency_overrides[deps.get_payload_store] = lambda: store
    application.dependency_overrides[deps.get_publisher] = lambda: publisher
    return application


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
        yield http_client
