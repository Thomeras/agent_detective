"""Judge calibration against human ground-truth labels (roadmap 2.7).

Slices tier1 terminal verdicts vs ground_truth_labels by
tier1_verdicts.judge_prompt_hash. KNOWN LIMITATION: the judge MODEL is not
recorded anywhere in the schema, so slices identify only the judge-prompt
version — two different judge models running the same prompts land in the
same slice.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from ..deps import get_repository
from ..repository import Repository

router = APIRouter(tags=["calibration"])

Repo = Annotated[Repository, Depends(get_repository)]


@router.get("/calibration")
async def calibration(repo: Repo) -> dict[str, Any]:
    rows = await repo.calibration_rows()

    slices: dict[Any, dict[str, int]] = {}
    for row in rows:
        prompt_hash = row.get("judge_prompt_hash")
        counts = slices.setdefault(
            prompt_hash,
            {"labels": 0, "agreements": 0, "judge_bad": 0, "label_bad": 0, "both_bad": 0},
        )
        label = row["label"]
        # None when the graph has no tier1 verdict (LEFT JOIN): the judge said
        # nothing, which can never agree and never counts as calling "bad".
        verdict = row.get("terminal_judge_verdict")
        counts["labels"] += 1
        if verdict == label:  # only 'ok'/'bad' can match the human label
            counts["agreements"] += 1
        if verdict == "bad":
            counts["judge_bad"] += 1
        if label == "bad":
            counts["label_bad"] += 1
        if verdict == "bad" and label == "bad":
            counts["both_bad"] += 1

    def slice_out(prompt_hash: Any, counts: dict[str, int]) -> dict[str, Any]:
        # Denominator 0 -> null. A rate nobody measured is not 0.0.
        precision = counts["both_bad"] / counts["judge_bad"] if counts["judge_bad"] else None
        recall = counts["both_bad"] / counts["label_bad"] if counts["label_bad"] else None
        return {
            "judge_prompt_hash": prompt_hash,
            "labels": counts["labels"],
            "agreements": counts["agreements"],
            "bad_precision": precision,
            "bad_recall": recall,
        }

    ordered = sorted(slices.items(), key=lambda item: (item[0] is None, item[0] or ""))
    return {"slices": [slice_out(prompt_hash, counts) for prompt_hash, counts in ordered]}
