"""Evidence-ledger verification (roadmap 2.6 audit trail non-repudiation).

The worker appends one row per persisted blame report:
  evidence_sha256 = sha256(canonical evidence JSON)
  chain_hash      = sha256((prev_chain_hash or "") + evidence_sha256)
  hmac_sig        = HMAC-SHA256(key=AUDIT_HMAC_KEY, msg=chain_hash)

Verification recomputes the chain from the first row through the target
report's row(s): each row's prev_hash must equal the previous row's stored
chain_hash, each chain_hash must recompute from (prev, evidence_sha256), and
the target rows' HMAC must verify. A tampered evidence_sha256 anywhere before
or at the target breaks the recomputation.

What this does and does not prove: the ledger proves the stored evidence hash
has not been silently rewritten since the report was persisted — it cannot
prove the evidence was correct in the first place. And with the default
dev-insecure HMAC key the signature proves nothing at all; AUDIT_HMAC_KEY MUST
be set in production.
"""

import hashlib
import hmac
from typing import Annotated, Any, Mapping

from fastapi import APIRouter, Depends

from ..config import Settings
from ..deps import get_repository, get_settings
from ..repository import Repository

router = APIRouter(tags=["audit"])

Repo = Annotated[Repository, Depends(get_repository)]


def verify_report_chain(
    rows: list[Mapping[str, Any]], report_id: int, hmac_key: str
) -> tuple[bool, bool, str]:
    """Returns (found, valid, detail); `rows` must be the full ledger in id order."""
    target_row_ids = [row["id"] for row in rows if row["report_id"] == report_id]
    if not target_row_ids:
        return False, False, "no evidence_ledger rows for this report"

    last_target_id = max(target_row_ids)
    prev_chain_hash: str | None = None
    for row in rows:
        if row["id"] > last_target_id:
            break  # rows after the target cannot invalidate it
        if (row["prev_hash"] or None) != prev_chain_hash:
            return True, False, f"prev_hash link broken at ledger row {row['id']}"
        expected_chain = hashlib.sha256(
            ((prev_chain_hash or "") + row["evidence_sha256"]).encode("utf-8")
        ).hexdigest()
        if row["chain_hash"] != expected_chain:
            return True, False, f"chain_hash does not recompute at ledger row {row['id']}"
        if row["report_id"] == report_id:
            expected_sig = hmac.new(
                hmac_key.encode("utf-8"), row["chain_hash"].encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected_sig, row["hmac_sig"]):
                return True, False, f"hmac signature invalid at ledger row {row['id']}"
        prev_chain_hash = row["chain_hash"]

    return (
        True,
        True,
        f"chain intact through ledger row {last_target_id} ({len(target_row_ids)} row(s) for report)",
    )


@router.get("/audit/verify/{report_id}")
async def audit_verify(
    report_id: int,
    repo: Repo,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    rows = await repo.list_ledger_rows()
    found, valid, detail = verify_report_chain(rows, report_id, settings.audit_hmac_key)
    return {
        "report_id": report_id,
        "found": found,
        "valid": valid,
        "chain_length": len(rows),
        "detail": detail,
    }
