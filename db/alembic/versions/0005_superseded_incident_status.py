"""Allow the superseded incident status.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-23

The latest completed tier2 analysis is authoritative for its graph: when a
re-analysis reclassifies a run (e.g. degraded_quality escalated to
latent_defect) — or comes back clean — the stale incident under the old
classification is marked ``superseded`` instead of paging forever next to the
new verdict. ``ck_incidents_status`` predates the value and rejected the row.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUSES_OLD = "'open','acknowledged','resolved'"
_STATUSES_NEW = _STATUSES_OLD + ",'superseded'"


def upgrade() -> None:
    op.drop_constraint("ck_incidents_status", "incidents", type_="check")
    op.create_check_constraint(
        "ck_incidents_status", "incidents", f"status IN ({_STATUSES_NEW})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_incidents_status", "incidents", type_="check")
    op.create_check_constraint(
        "ck_incidents_status", "incidents", f"status IN ({_STATUSES_OLD})"
    )
