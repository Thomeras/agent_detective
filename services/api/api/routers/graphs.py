"""Graph endpoints: list, cytoscape-shaped detail, run payloads, manual analyze."""

from datetime import datetime, timezone
from typing import Annotated, Any
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
