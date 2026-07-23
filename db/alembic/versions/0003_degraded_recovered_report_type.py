"""Allow the degraded_recovered report type.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-23

The blame engine gained the ``degraded_recovered`` verdict (a node that
underperformed but every downstream step and the terminal deliverable
recovered — a near-miss, not a live break). The ``ck_blame_reports_type``
check predates it and rejected the row.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TYPES_OLD = (
    "'cut_point','multi_culprit','composition_failure',"
    "'loop_detected','root_cause_external','unclassified',"
    "'verification_gap'"
)
_TYPES_NEW = _TYPES_OLD + ",'degraded_recovered'"


def upgrade() -> None:
    op.drop_constraint("ck_blame_reports_type", "blame_reports", type_="check")
    op.create_check_constraint(
        "ck_blame_reports_type", "blame_reports", f"report_type IN ({_TYPES_NEW})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_blame_reports_type", "blame_reports", type_="check")
    op.create_check_constraint(
        "ck_blame_reports_type", "blame_reports", f"report_type IN ({_TYPES_OLD})"
    )
