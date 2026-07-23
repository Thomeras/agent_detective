"""Circuit-breaker state read endpoint (roadmap 2.3).

Honest framing: a breaker row is a RECORDED decision by the worker. Agent
Detective observes and cannot stop anything — enforcement happens only if the
instrumented side polls this endpoint (e.g. detective_sdk's optional
should_halt hook) and chooses to honor it.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ..deps import get_repository
from ..repository import Repository
from ..serializers import json_row

router = APIRouter(tags=["control"])

Repo = Annotated[Repository, Depends(get_repository)]


@router.get("/control/breakers")
async def list_breakers(repo: Repo) -> dict[str, Any]:
    rows = await repo.list_breakers()
    return {
        "breakers": [
            json_row(row, ["scope_kind", "scope_value", "state", "reason", "opened_at", "updated_at"])
            for row in rows
        ]
    }
