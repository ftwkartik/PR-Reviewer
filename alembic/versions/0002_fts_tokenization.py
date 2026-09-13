"""index dotted identifiers as separate words in the lexical search column

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_OLD = "to_tsvector('simple', coalesce(qualified_name,'') || ' ' || coalesce(signature,'') || ' ' || content)"
_NEW = (
    "to_tsvector('simple', translate(coalesce(qualified_name,'') || ' ' || "
    "coalesce(signature,'') || ' ' || content, '.', ' '))"
)


def _swap(expr: str) -> None:
    op.drop_index("ix_code_chunks_fts", table_name="code_chunks", postgresql_using="gin")
    op.drop_column("code_chunks", "fts")
    op.add_column(
        "code_chunks",
        sa.Column("fts", postgresql.TSVECTOR(), sa.Computed(expr, persisted=True), nullable=True),
    )
    op.create_index(
        "ix_code_chunks_fts", "code_chunks", ["fts"], unique=False, postgresql_using="gin"
    )


def upgrade() -> None:
    _swap(_NEW)


def downgrade() -> None:
    _swap(_OLD)
