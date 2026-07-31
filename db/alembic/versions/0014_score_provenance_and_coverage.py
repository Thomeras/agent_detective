"""Every score names its instrument, every total names its coverage.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-30

A forensic score has to be able to say what measured it. Until now the judge
MODEL existed only in worker config and in the outgoing HTTP request, so 0.4
from a cheap model and 0.4 from a frontier one were the same row in the
database — and /calibration, /agents/leaderboard and version-diff compared
across incommensurable measurements with no way to tell.

``judge_model`` records it beside the existing ``judge_prompt_hash`` (0009) on
both verdict tables and on the run whose judge component it produced.

``agent_runs.score_weights`` records the weights ACTUALLY used after renormalization.
A missing channel redistributed its weight silently (schema absent -> judge
0.40 becomes 0.727), so a single-channel score was indistinguishable from a
three-channel one.

``blame_reports.cost_coverage`` records how many affected runs carried a price,
so a total summed over 6 of 28 runs reads as the lower bound it is.

All NULL for pre-existing rows: unmeasured, not a measured zero.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("judge_model", sa.Text(), nullable=True))
    op.add_column(
        "agent_runs", sa.Column("score_weights", postgresql.JSONB(), nullable=True)
    )
    op.add_column("blame_reports", sa.Column("judge_model", sa.Text(), nullable=True))
    op.add_column(
        "blame_reports", sa.Column("cost_coverage", postgresql.JSONB(), nullable=True)
    )
    op.add_column("tier1_verdicts", sa.Column("judge_model", sa.Text(), nullable=True))
    # Calibration slices by (prompt_hash, model) — without the model in the key
    # two different judges on the same prompts land in the same slice.
    op.create_index(
        "ix_tier1_verdicts_judge_model",
        "tier1_verdicts",
        ["judge_prompt_hash", "judge_model"],
    )


def downgrade() -> None:
    op.drop_index("ix_tier1_verdicts_judge_model", table_name="tier1_verdicts")
    op.drop_column("tier1_verdicts", "judge_model")
    op.drop_column("blame_reports", "cost_coverage")
    op.drop_column("blame_reports", "judge_model")
    op.drop_column("agent_runs", "score_weights")
    op.drop_column("agent_runs", "judge_model")
