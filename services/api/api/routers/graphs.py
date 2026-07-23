"""Graph endpoints: list, cytoscape-shaped detail, run payloads, manual analyze,
version diff, policy decisions (shadow observations) and human feedback."""

from datetime import datetime, timezone
from typing import Annotated, Any, Mapping
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_payload_store, get_publisher, get_repository
from ..payloads import MinioPayloadStore
from ..repository import Repository
from ..serializers import graph_summary, json_row, run_edge, run_node
from ..streams import RedisStreamPublisher

router = APIRouter(tags=["graphs"])

Repo = Annotated[Repository, Depends(get_repository)]


class AnalyzeResponse(BaseModel):
    dedup_key: str
    stream_id: str


@router.get("/graphs")
async def list_graphs(
    repo: Repo,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows = await repo.list_graphs(limit=limit, offset=offset)
    return {"graphs": [graph_summary(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/graphs/{graph_id}")
async def get_graph(graph_id: UUID, repo: Repo) -> dict[str, Any]:
    graph = await repo.get_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="graph not found")
    runs, graph_edges = await repo.list_runs(graph_id), await repo.list_edges(graph_id)
    return graph_summary(graph) | {
        "finalized_at": json_row(graph, ["finalized_at"])["finalized_at"],
        "nodes": [run_node(row) for row in runs],
        "edges": [run_edge(row) for row in graph_edges],
    }


@router.get("/graphs/{graph_id}/payloads/{run_id}")
async def get_run_payloads(
    graph_id: UUID,
    run_id: UUID,
    repo: Repo,
    store: Annotated[MinioPayloadStore, Depends(get_payload_store)],
) -> dict[str, Any]:
    run = await repo.get_run(graph_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def payload(side: str) -> dict[str, Any]:
        inline, overflow_ref, byte_count = (
            run.get(f"{side}_inline"),
            run.get(f"{side}_overflow_ref"),
            run.get(f"{side}_bytes"),
        )
        if inline is not None:
            return {"source": "inline", "content": inline, "bytes": byte_count}
        if overflow_ref is not None:
            return {"source": "overflow", "content": await store.get_text(overflow_ref), "bytes": byte_count}
        return {"source": "none", "content": None, "bytes": byte_count}

    return {
        "graph_id": str(graph_id),
        "run_id": str(run_id),
        "input": await payload("input"),
        "output": await payload("output"),
    }


@router.post("/graphs/{graph_id}/analyze", response_model=AnalyzeResponse)
async def analyze_graph(
    graph_id: UUID,
    repo: Repo,
    publisher: Annotated[RedisStreamPublisher, Depends(get_publisher)],
) -> AnalyzeResponse:
    """Manual tier2 trigger (build spec section 4.1)."""
    graph = await repo.get_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="graph not found")
    dedup_key = f"{graph_id}:manual:{uuid4()}"
    # tier1_verdict_ref points at the tier1_verdicts PK when a verdict exists.
    has_verdict = await repo.has_tier1_verdict(graph_id)
    message = {
        "schema_version": "1",
        "graph_id": str(graph_id),
        "trigger": "manual",
        "dedup_key": dedup_key,
        "tier1_verdict_ref": str(graph_id) if has_verdict else "",
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    stream_id = await publisher.publish_tier2(message)
    return AnalyzeResponse(dedup_key=dedup_key, stream_id=stream_id)


# --- Version diff (roadmap 2.1: "why did it work yesterday?") ---

_IDENTITY_FIELDS = ["agent_version", "model_name", "prompt_hash", "tool_schema_hash"]

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _latest_identity_per_agent(runs: list[Mapping[str, Any]]) -> dict[Any, dict[str, Any]]:
    """Per agent_name, the identity fields of the latest run by started_at.

    Runs without started_at sort earliest; ties break on run_id for determinism.
    """
    latest_run: dict[Any, Mapping[str, Any]] = {}

    def order(run: Mapping[str, Any]) -> tuple[datetime, str]:
        return (run.get("started_at") or _EPOCH, str(run["run_id"]))

    for run in runs:
        name = run.get("agent_name")
        if name not in latest_run or order(run) > order(latest_run[name]):
            latest_run[name] = run
    return {
        name: {field: run.get(field) for field in _IDENTITY_FIELDS}
        for name, run in latest_run.items()
    }


@router.get("/graphs/{graph_id}/version-diff")
async def version_diff(graph_id: UUID, repo: Repo, against: str = "last_clean") -> dict[str, Any]:
    graph = await repo.get_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="graph not found")

    if against == "last_clean":
        against_mode = "last_clean"
        # Most recent OTHER finalized graph with zero incidents rows; None when
        # no such graph exists (then every baseline below is null — no guessing).
        baseline_graph = await repo.find_last_clean_graph(exclude_graph_id=graph_id)
        baseline_id = baseline_graph["graph_id"] if baseline_graph is not None else None
    else:
        against_mode = "explicit"
        try:
            baseline_id = UUID(against)
        except ValueError:
            raise HTTPException(status_code=400, detail="against must be 'last_clean' or a graph UUID")
        if await repo.get_graph(baseline_id) is None:
            raise HTTPException(status_code=404, detail="against graph not found")

    current = _latest_identity_per_agent(await repo.list_runs(graph_id))
    baseline = _latest_identity_per_agent(await repo.list_runs(baseline_id)) if baseline_id else {}

    per_agent = []
    for agent_name in sorted(current, key=str):
        cur, base = current[agent_name], baseline.get(agent_name)
        # changed = fields whose values differ; null-vs-value counts as changed.
        # With no baseline (agent absent / no clean graph) there is nothing to
        # diff against, so changed stays empty and baseline is null.
        changed = [f for f in _IDENTITY_FIELDS if base is not None and cur[f] != base[f]]
        per_agent.append(
            {"agent_name": agent_name, "current": cur, "baseline": base, "changed": changed}
        )
    return {
        "graph_id": str(graph_id),
        "against": str(baseline_id) if baseline_id else None,
        "against_mode": against_mode,
        "per_agent": per_agent,
    }


# --- Policy decisions (roadmap 2.2, shadow mode) ---


@router.get("/graphs/{graph_id}/policy-decisions")
async def get_policy_decisions(graph_id: UUID, repo: Repo) -> dict[str, Any]:
    """Shadow-mode gate observations: 'would_block'/'would_warn' annotations
    recorded post-hoc — Agent Detective observed, it did not intercept."""
    if await repo.get_graph(graph_id) is None:
        raise HTTPException(status_code=404, detail="graph not found")
    rows = await repo.list_policy_decisions(graph_id)
    return {
        "decisions": [
            json_row(row, ["rule_name", "decision", "detail", "mode", "created_at"]) for row in rows
        ]
    }


# --- Human feedback (roadmap 2.7: ground-truth labels) ---


class FeedbackBody(BaseModel):
    # label is validated by hand so a bad value returns the contract's 400
    # (pydantic Literal would produce a 422 instead).
    label: str
    culprit_run_id: str | None = None
    note: str | None = None


@router.post("/graphs/{graph_id}/feedback")
async def post_feedback(graph_id: UUID, body: FeedbackBody, repo: Repo) -> dict[str, Any]:
    if body.label not in ("ok", "bad"):
        raise HTTPException(status_code=400, detail="label must be 'ok' or 'bad'")
    culprit_run_id: UUID | None = None
    if body.culprit_run_id is not None:
        try:
            culprit_run_id = UUID(body.culprit_run_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="culprit_run_id must be a UUID")
    if await repo.get_graph(graph_id) is None:
        raise HTTPException(status_code=404, detail="graph not found")
    label_id = await repo.insert_feedback(graph_id, body.label, culprit_run_id, body.note)
    return {"id": label_id}
