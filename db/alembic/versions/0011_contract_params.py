"""Convention lane into the deterministic contract channel.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-27

``agent_runs.contract_params`` carries the raw ``agent_detective.contract_params``
opener-span attribute: a JSON object of parameters the run's input is
contractually bound to (e.g. {"file_type": "pdf"}). Foreign pipelines whose
payloads are prose/code (so the input/output JSON diff has nothing to parse —
the measured CrewAI wild-trace gap) can declare contract params with one span
attribute and get deterministic contract checking without adopting the full
SDK payload conventions.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("contract_params", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "contract_params")
