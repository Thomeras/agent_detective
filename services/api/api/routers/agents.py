"""Per-agent leaderboard (cost, failure rate, average quality score) and
per-version stats (roadmap 2.1 leaderboard-by-version, 2.4 canary compare)."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from ..deps import get_repository
from ..repository import Repository
from ..serializers import json_row, json_value

router = APIRouter(tags=["agents"])

Repo = Annotated[Repository, Depends(get_repository)]


@router.get("/agents/leaderboard")
async def leaderboard(
    repo: Repo,
    group_by: Annotated[Literal["version"] | None, Query()] = None,
) -> dict[str, Any]:
    if group_by == "version":
        rows = await repo.leaderboard_by_version()
        fields = [
            "agent_name",
            "agent_version",
            "model_name",
            "prompt_hash",
            "total_cost_usd",
            "run_count",
            # SUM(cost_usd) skips NULLs, so the total needs its denominator to
            # read as a lower bound rather than a price.
            "priced_run_count",
            "failure_rate",
            "avg_quality_score",
        ]
    else:
        # Default behavior unchanged without the param.
        rows = await repo.leaderboard()
        fields = [
            "agent_name", "total_cost_usd", "run_count", "priced_run_count",
            "failure_rate", "avg_quality_score",
        ]
    return {"agents": [json_row(row, fields) for row in rows]}


@router.get("/agents/{agent_name}/versions/compare")
async def versions_compare(agent_name: str, base: str, candidate: str, repo: Repo) -> dict[str, Any]:
    """Canary comparison over tier1 data (the honest v1: full blame data exists
    only for flagged/sampled graphs, so rates come from tier1_verdicts).
    Rates are null when no graph of a version has a tier1 verdict — never 0.0."""
    base_stats = await repo.agent_version_stats(agent_name, base)
    candidate_stats = await repo.agent_version_stats(agent_name, candidate)
    return {
        "agent_name": agent_name,
        "base": {key: json_value(value) for key, value in base_stats.items()},
        "candidate": {key: json_value(value) for key, value in candidate_stats.items()},
    }
