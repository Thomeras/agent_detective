"""Terminal rubric split: form dimension + terminal_defect_unlocalized.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-24

The tier1 terminal judge verdict is split into two independent dimensions:
CONTENT (substance vs goal — stays in the existing verdict/score/reasoning
columns) and FORM (does the deliverable's explicitly requested form match what
shipped — new JSONB column ``terminal_form`` holding
{"verdict","requirement","observed","reasoning"}, where ``requirement`` is the
judge's VERBATIM quote from the initial input, the provenance anchor for
reconciling deterministic contract references against the user's actual ask).

The split also introduces the engine outcome ``terminal_defect_unlocalized``:
a content-bad terminal whose only origin candidate is a deterministic-channel
(contract) origin with untouched content — the old verdict was a cut_point
claiming a content defect its own evidence did not show. The CHECK constraint
predates the value and rejected the rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TYPES_OLD = (
    "'cut_point','multi_culprit','composition_failure',"
    "'loop_detected','root_cause_external','unclassified',"
    "'verification_gap','degraded_recovered','shipped_with_latent_defect'"
)
_TYPES_NEW = _TYPES_OLD + ",'terminal_defect_unlocalized'"


def upgrade() -> None:
    op.add_column("tier1_verdicts", sa.Column("terminal_form", JSONB, nullable=True))
    op.drop_constraint("ck_blame_reports_type", "blame_reports", type_="check")
    op.create_check_constraint(
        "ck_blame_reports_type", "blame_reports", f"report_type IN ({_TYPES_NEW})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_blame_reports_type", "blame_reports", type_="check")
    op.create_check_constraint(
        "ck_blame_reports_type", "blame_reports", f"report_type IN ({_TYPES_OLD})"
    )
    op.drop_column("tier1_verdicts", "terminal_form")
