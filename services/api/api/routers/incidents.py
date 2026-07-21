"""Incident inbox endpoints: list, detail (with latest blame report), status patch."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_repository
from ..repository import Repository
from ..serializers import incident_detail, incident_summary

router = APIRouter(tags=["incidents"])

Repo = Annotated[Repository, Depends(get_repository)]

IncidentStatus = Literal["open", "acknowledged", "resolved"]


class IncidentPatch(BaseModel):
    status: IncidentStatus


@router.get("/incidents")
async def list_incidents(
    repo: Repo,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows = await repo.list_incidents(limit=limit, offset=offset)
    return {"incidents": [incident_summary(row) for row in rows], "limit": limit, "offset": offset}


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: int, repo: Repo) -> dict[str, Any]:
    incident = await repo.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    report = await repo.get_latest_report(incident_id)
    return incident_detail(incident, report)


@router.patch("/incidents/{incident_id}")
async def patch_incident(incident_id: int, body: IncidentPatch, repo: Repo) -> dict[str, Any]:
    updated = await repo.update_incident_status(incident_id, body.status)
    if updated is None:
        raise HTTPException(status_code=404, detail="incident not found")
    report = await repo.get_latest_report(incident_id)
    return incident_detail(updated, report)
