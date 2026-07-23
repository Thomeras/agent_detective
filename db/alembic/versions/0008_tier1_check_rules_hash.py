"""Stamp tier1 verdicts with the rule-set fingerprint they ran under.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-23

Tier2 reconciliation discards a deterministically-decided ``bad`` terminal
verdict when its basis no longer reproduces (``stale``). Without provenance it
cannot say WHY: a changed registered rule set and a genuinely diverged
artifact/payload look identical. ``check_rules_hash`` records the fingerprint
(``signals.check_rules_fingerprint``: canonical rules + min_artifact_bytes,
12 hex chars) at verdict time — a differing fingerprint proves a rule change,
an identical one proves representation divergence. NULL on verdicts that
predate stamping (reconciliation reports "cause unknown" for those).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tier1_verdicts", sa.Column("check_rules_hash", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tier1_verdicts", "check_rules_hash")
