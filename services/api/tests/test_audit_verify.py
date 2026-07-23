"""GET /audit/verify/{report_id}: chain recomputation over evidence_ledger."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
KEY = b"dev-insecure-key"  # Settings default; the app fixture uses Settings()


def make_ledger(entries: list[tuple[int, dict]]) -> list[dict]:
    """Build a valid chain exactly per the frozen worker-side algorithm."""
    rows = []
    prev_hash: str | None = None
    for row_id, (report_id, evidence) in enumerate(entries, start=1):
        canonical = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        evidence_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        chain_hash = hashlib.sha256(((prev_hash or "") + evidence_sha256).encode("utf-8")).hexdigest()
        hmac_sig = hmac.new(KEY, chain_hash.encode("utf-8"), hashlib.sha256).hexdigest()
        rows.append(
            {
                "id": row_id,
                "report_id": report_id,
                "evidence_sha256": evidence_sha256,
                "prev_hash": prev_hash,
                "chain_hash": chain_hash,
                "hmac_sig": hmac_sig,
                "created_at": T0,
            }
        )
        prev_hash = chain_hash
    return rows


@pytest.fixture
def ledger(repo):
    rows = make_ledger(
        [
            (10, {"drops": [{"run_id": "a", "from": 0.9, "to": 0.4}]}),
            (11, {"judge_reasoning": "fabricated prices", "háček": "diakritika"}),
            (12, {"terminal_verdict": "bad"}),
        ]
    )
    repo.ledger_rows = rows
    return rows


async def test_verify_happy_path(client, ledger):
    response = await client.get("/audit/verify/11")
    assert response.status_code == 200
    body = response.json()
    assert body["report_id"] == 11
    assert body["found"] is True
    assert body["valid"] is True
    assert body["chain_length"] == 3
    assert "intact" in body["detail"]


async def test_verify_unknown_report(client, ledger):
    response = await client.get("/audit/verify/999")
    body = response.json()
    assert body["found"] is False
    assert body["valid"] is False
    assert body["chain_length"] == 3


async def test_verify_empty_ledger(client, repo):
    repo.ledger_rows = []
    body = (await client.get("/audit/verify/10")).json()
    assert body == {
        "report_id": 10,
        "found": False,
        "valid": False,
        "chain_length": 0,
        "detail": "no evidence_ledger rows for this report",
    }


async def test_tampered_evidence_hash_invalidates(client, ledger):
    # Tamper with the stored evidence hash of the target's row: the chain hash
    # no longer recomputes, so verification must fail.
    ledger[1]["evidence_sha256"] = "0" * 64
    body = (await client.get("/audit/verify/11")).json()
    assert body["found"] is True
    assert body["valid"] is False
    assert "does not recompute" in body["detail"]


async def test_tampered_ancestor_invalidates_descendant(client, ledger):
    ledger[0]["evidence_sha256"] = "f" * 64
    body = (await client.get("/audit/verify/12")).json()
    assert body["valid"] is False


async def test_tampered_hmac_invalidates(client, ledger):
    ledger[2]["hmac_sig"] = "0" * 64
    body = (await client.get("/audit/verify/12")).json()
    assert body["found"] is True
    assert body["valid"] is False
    assert "hmac" in body["detail"]


async def test_tamper_after_target_does_not_invalidate_target(client, ledger):
    # Rows appended after the target cannot retroactively invalidate it.
    ledger[2]["evidence_sha256"] = "0" * 64
    body = (await client.get("/audit/verify/10")).json()
    assert body["valid"] is True
