"""Per-agent leaderboard (cost, failure rate, average quality score)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ..deps import get_repository
from ..repository import Repository
from ..serializers import json_row

router = APIRouter(tags=["agents"])


@router.get("/agents/leaderboard")
async def leaderboard(repo: Annotated[Repository, Depends(get_repository)]) -> dict[str, Any]:
    rows = await repo.leaderboard()
    agents = [
        json_row(row, ["agent_name", "total_cost_usd", "run_count", "failure_rate", "avg_quality_score"])
        for row in rows
    ]
    return {"agents": agents}
