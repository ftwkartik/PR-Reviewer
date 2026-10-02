"""review_findings.reject_reason: VARCHAR(64) -> TEXT (synthesis reasons are longer)

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("review_findings", "reject_reason", type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column(
        "review_findings",
        "reject_reason",
        type_=sa.String(64),
        existing_nullable=True,
        postgresql_using="left(reject_reason, 64)",
    )
