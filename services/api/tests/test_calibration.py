"""GET /calibration: tier1 terminal verdict vs human labels, sliced by judge_prompt_hash."""

import uuid

import pytest

pytestmark = pytest.mark.anyio

G1 = uuid.UUID("31111111-1111-1111-1111-111111111111")
G2 = uuid.UUID("32222222-2222-2222-2222-222222222222")
G3 = uuid.UUID("33333333-3333-3333-3333-333333333333")
G4 = uuid.UUID("34444444-4444-4444-4444-444444444444")


def label_row(label_id: int, graph_id: uuid.UUID, label: str) -> dict:
    return {"id": label_id, "graph_id": graph_id, "label": label, "culprit_run_id": None, "source": "human", "note": None, "created_at": None}


async def test_calibration_slices_by_judge_prompt_hash(client, repo, verdict_factory):
    repo.verdicts = {
        v["graph_id"]: v
        for v in [
            # hash-A: judge bad/label bad (agree), judge ok/label bad (miss)
            verdict_factory(G1, terminal_judge_verdict="bad", judge_prompt_hash="hash-a"),
            verdict_factory(G2, terminal_judge_verdict="ok", judge_prompt_hash="hash-a"),
            # hash-B: judge ok/label ok — judge never said bad, nobody labeled bad
            verdict_factory(G3, terminal_judge_verdict="ok", judge_prompt_hash="hash-b"),
        ]
    }
    repo.labels = [
        label_row(1, G1, "bad"),
        label_row(2, G2, "bad"),
        label_row(3, G3, "ok"),
        label_row(4, G4, "bad"),  # G4 has no tier1 verdict -> NULL slice
    ]

    response = await client.get("/calibration")
    assert response.status_code == 200
    slices = {s["judge_prompt_hash"]: s for s in response.json()["slices"]}
    assert set(slices) == {"hash-a", "hash-b", None}

    slice_a = slices["hash-a"]
    assert slice_a["labels"] == 2
    assert slice_a["agreements"] == 1
    assert slice_a["bad_precision"] == pytest.approx(1.0)  # 1 judge-bad, labeled bad
    assert slice_a["bad_recall"] == pytest.approx(0.5)  # 2 labeled bad, judge caught 1

    # Denominator honesty: no judge-bad calls and no bad labels -> null, NOT 0.0.
    slice_b = slices["hash-b"]
    assert slice_b["labels"] == 1
    assert slice_b["agreements"] == 1
    assert slice_b["bad_precision"] is None
    assert slice_b["bad_recall"] is None

    # Label without a tier1 verdict: judge said nothing, so no agreement and
    # a missed bad label (recall 0.0 here is measured — the denominator is 1).
    null_slice = slices[None]
    assert null_slice["labels"] == 1
    assert null_slice["agreements"] == 0
    assert null_slice["bad_precision"] is None  # judge never called bad in this slice
    assert null_slice["bad_recall"] == pytest.approx(0.0)


async def test_calibration_no_labels(client, repo):
    response = await client.get("/calibration")
    assert response.status_code == 200
    assert response.json() == {"slices": []}
