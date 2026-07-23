"""Allow the verification_gap report type.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-22

The blame engine gained the ``verification_gap`` verdict (a verifier whose PASS
was wrong — scored wrong by the role-aware judge, or deduced from a bad
terminal). The ``ck_blame_reports_type`` check predates it and rejected the row.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TYPES_OLD = (
    "'cut_point','multi_culprit','composition_failure',"
    "'loop_detected','root_cause_external','unclassified'"
)
_TYPES_NEW = _TYPES_OLD + ",'verification_gap'"


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
