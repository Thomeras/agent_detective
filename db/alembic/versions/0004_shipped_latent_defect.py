"""Allow the shipped_with_latent_defect report type and latent_defect trigger.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-23

Tier2 escalates ``degraded_recovered`` to ``shipped_with_latent_defect`` when a
deterministic contract breach is VERIFIED to have propagated into the shipped
deliverable (the terminal judge verifies content, not carried contract
parameters, so its ok verdict cannot catch this). The escalated verdict gets its
own high-severity incident trigger ``latent_defect`` so alerting can tell a
silent failure in production apart from ordinary degraded quality — and from the
low-priority degraded_recovered near-miss. Both CHECK constraints predate the
values and rejected the rows.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TYPES_OLD = (
    "'cut_point','multi_culprit','composition_failure',"
    "'loop_detected','root_cause_external','unclassified',"
    "'verification_gap','degraded_recovered'"
)
_TYPES_NEW = _TYPES_OLD + ",'shipped_with_latent_defect'"

_TRIGGERS_OLD = (
    "'terminal_failure','degraded_quality','cost_overrun','loop_detected','manual'"
)
_TRIGGERS_NEW = _TRIGGERS_OLD + ",'latent_defect'"


def upgrade() -> None:
    op.drop_constraint("ck_blame_reports_type", "blame_reports", type_="check")
    op.create_check_constraint(
        "ck_blame_reports_type", "blame_reports", f"report_type IN ({_TYPES_NEW})"
    )
    op.drop_constraint("ck_incidents_trigger", "incidents", type_="check")
    op.create_check_constraint(
        "ck_incidents_trigger", "incidents", f"trigger IN ({_TRIGGERS_NEW})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_incidents_trigger", "incidents", type_="check")
    op.create_check_constraint(
        "ck_incidents_trigger", "incidents", f"trigger IN ({_TRIGGERS_OLD})"
    )
    op.drop_constraint("ck_blame_reports_type", "blame_reports", type_="check")
    op.create_check_constraint(
        "ck_blame_reports_type", "blame_reports", f"report_type IN ({_TYPES_OLD})"
    )
