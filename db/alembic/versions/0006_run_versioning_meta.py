"""Run versioning metadata + tier1 verdict constraint drift fix.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-23

Three changes:

1. Per-run identity for "why did it work yesterday?" diffs (design:
   docs/deterministic-signals.md, B1): ``agent_runs`` gains nullable Text
   columns ``model_name`` (mapped from ``gen_ai.request.model``) and
   ``prompt_hash`` (mapped from ``agent_detective.prompt_hash``). Both are
   never invented — NULL when the instrumentation did not send them; the
   ``ON CONFLICT DO NOTHING`` run upsert means already-ingested runs stay
   NULL (forward-only).

2. Out-of-band artifact integrity metadata: ``agent_runs`` gains a nullable
   Text column ``artifact_meta`` holding the raw
   ``agent_detective.artifact_meta`` span attribute (compact JSON string —
   array of {"path","size","sha256","declared_ext","detected_kind",
   "parse_ok","nonempty"}) from the opening AGENT span. The worker reads it
   from the run row, never from forgeable payload text. Deliberately Text,
   not JSONB: the worker parses tolerantly, so a malformed emitter string
   must not fail ingest.

3. Latent drift fix: ``ck_tier1_verdicts_verdict`` was created by
   0001_initial_schema.py allowing only ``('ok','bad','error')``, but tier1
   has emitted ``not_checkable`` since the phantom-terminal fix (a terminal
   whose deliverable cannot be judged is not silently 'ok'). The constraint
   is dropped and recreated with ``not_checkable`` included.

Downgrade is deliberately lossy on the verdict constraint: recreating the
three-value constraint would raise CheckViolation while ``not_checkable``
rows exist, so downgrade first rewrites ``not_checkable`` ->
``error`` (the closest pre-0006 meaning: "no usable judgement") and only
then restores the original constraint. The original verdicts are not
recoverable after downgrade.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VERDICTS_OLD = "'ok','bad','error'"
_VERDICTS_NEW = _VERDICTS_OLD + ",'not_checkable'"


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("model_name", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("prompt_hash", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("artifact_meta", sa.Text(), nullable=True))

    op.drop_constraint("ck_tier1_verdicts_verdict", "tier1_verdicts", type_="check")
    op.create_check_constraint(
        "ck_tier1_verdicts_verdict",
        "tier1_verdicts",
        f"terminal_judge_verdict IN ({_VERDICTS_NEW})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tier1_verdicts_verdict", "tier1_verdicts", type_="check")
    # Deliberately lossy (see module docstring): 'not_checkable' rows would
    # violate the recreated three-value constraint, so they are rewritten to
    # 'error' first.
    op.execute(
        "UPDATE tier1_verdicts SET terminal_judge_verdict = 'error' "
        "WHERE terminal_judge_verdict = 'not_checkable'"
    )
    op.create_check_constraint(
        "ck_tier1_verdicts_verdict",
        "tier1_verdicts",
        f"terminal_judge_verdict IN ({_VERDICTS_OLD})",
    )

    op.drop_column("agent_runs", "artifact_meta")
    op.drop_column("agent_runs", "prompt_hash")
    op.drop_column("agent_runs", "model_name")
