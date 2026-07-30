"""Output contracts: the write path for the schema scoring channel.

`output_contracts` had readers (tier1/tier2 call read_output_contracts) and no
writer, so on a fresh install the table is empty, `evaluate_schema` returns None
for every node, and the composite silently renormalizes the judge from 0.40 to
0.727 — three independent channels on paper, two in practice. These endpoints
are that missing writer, plus `/contracts/suggest`, which derives a candidate
schema from payloads Detective already stored so registering one does not start
with a blank file.
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..contracts import check_contract_schema, infer_schema
from ..deps import get_payload_store, get_repository
from ..payloads import MinioPayloadStore
from ..repository import Repository
from ..serializers import json_row

router = APIRouter(tags=["contracts"])

Repo = Annotated[Repository, Depends(get_repository)]

# A key must survive this many independent generations before "it was in every
# sample" means anything. Below ~5 the claim is mostly luck (with 3 samples a
# key absent 40% of the time is still present in all three about a fifth of the
# time); far above it, a fresh install with a handful of demo runs could never
# use the endpoint at all. The counts ride along in the response so the reader
# judges the evidence rather than trusting the threshold.
DEFAULT_MIN_SAMPLES = 5

_CONTRACT_FIELDS = ["id", "agent_name", "agent_version_pattern", "json_schema", "created_at"]


class ContractBody(BaseModel):
    agent_name: str
    # Empty/absent means "every version": scoring only fnmatch-es a non-empty
    # pattern. Stored as an explicit '*' so a listing never reads as a mistake.
    agent_version_pattern: str | None = None
    json_schema: dict[str, Any]


@router.get("/contracts")
async def list_contracts(repo: Repo) -> dict[str, Any]:
    rows = await repo.list_output_contracts()
    return {"contracts": [json_row(row, _CONTRACT_FIELDS) for row in rows]}


@router.post("/contracts")
async def put_contract(body: ContractBody, repo: Repo) -> dict[str, Any]:
    """Create or replace the contract for one (agent_name, version pattern)."""
    agent_name = body.agent_name.strip()
    if not agent_name:
        # Scoring selects contracts by exact agent_name; a blank one is a row
        # that can never match a run — a contract that exists and does nothing.
        raise HTTPException(status_code=400, detail="agent_name must not be empty")
    pattern = (body.agent_version_pattern or "").strip() or "*"

    check = check_contract_schema(body.json_schema)
    if not check.ok:
        raise HTTPException(
            status_code=400,
            detail="json_schema is not enforceable: " + "; ".join(check.problems),
        )

    row = await repo.replace_output_contract(agent_name, pattern, body.json_schema)
    return json_row(row, _CONTRACT_FIELDS) | {
        "replaced": row.get("replaced", 0),
        # Keywords the scoring engine does not implement. Stored as given, but
        # they constrain nothing — saying so beats a contract read as stricter
        # than it is.
        "ignored_keywords": check.ignored_keywords,
    }


@router.get("/contracts/suggest")
async def suggest_contract(
    repo: Repo,
    store: Annotated[MinioPayloadStore, Depends(get_payload_store)],
    agent_name: Annotated[str, Query(min_length=1)],
    min_samples: Annotated[int, Query(ge=3, le=200)] = DEFAULT_MIN_SAMPLES,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Propose a JSON Schema from this agent's stored outputs — or refuse.

    `contract` is a ready POST /contracts body when the samples support one and
    null when they do not; `reason` then says what the payloads failed to agree
    on. Nothing here is guessed: required means "in every usable sample", types
    are the types observed, and no enum, format, range or nested constraint is
    ever invented from a sample this small.
    """
    runs = await repo.list_agent_outputs(agent_name, limit)

    async def output_text(run: Any) -> str | None:
        # Same inline-or-overflow resolution the run payloads endpoint serves.
        inline = run.get("output_inline")
        if inline is not None:
            return inline
        ref = run.get("output_overflow_ref")
        return await store.get_text(ref) if ref is not None else None

    resolved = await asyncio.gather(
        *(output_text(run) for run in runs), return_exceptions=True
    )
    # An overflow payload we could not fetch is an unusable sample, not a
    # failed request: the honest counts below already account for it.
    outputs = [text if isinstance(text, str) else None for text in resolved]

    inference = infer_schema(outputs, min_samples)
    contract = (
        {
            "agent_name": agent_name,
            "agent_version_pattern": "*",
            "json_schema": inference.schema,
        }
        if inference.schema is not None
        else None
    )
    return {
        "agent_name": agent_name,
        "min_samples": min_samples,
        "samples": {
            "runs_examined": inference.runs_examined,
            "runs_with_output": inference.runs_with_output,
            "usable_samples": inference.usable_samples,
            "failed_runs": sum(1 for run in runs if run.get("status") == "failed"),
        },
        "keys": inference.keys,
        "contract": contract,
        "reason": inference.reason,
    }
