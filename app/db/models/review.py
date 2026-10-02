import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)  # fmt: skip
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, created_at, uuid_pk


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL")
    )
    payload_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = created_at()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))
    review_job_id: Mapped[uuid.UUID | None] = mapped_column()


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        # One live job per (repo, PR, head SHA); failed/cancelled/stale jobs may be re-run.
        Index(
            "uq_review_jobs_live_head",
            "repository_id",
            "pull_number",
            "head_sha",
            unique=True,
            postgresql_where=text("status NOT IN ('FAILED','CANCELLED','STALE')"),
        ),  # fmt: skip
        Index("ix_review_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    pull_number: Mapped[int] = mapped_column(Integer)
    base_sha: Mapped[str | None] = mapped_column(String(40))
    head_sha: Mapped[str] = mapped_column(String(40))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="SET NULL")
    )
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    trigger: Mapped[str] = mapped_column(String(16), default="webhook")
    delivery_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    status_detail: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    timings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    github_review_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewFinding(Base):
    __tablename__ = "review_findings"
    __table_args__ = (
        Index("ix_review_findings_job_status", "review_job_id", "status"),
        Index("ix_review_findings_fingerprint", "fingerprint"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_jobs.id", ondelete="CASCADE")
    )
    path: Mapped[str] = mapped_column(Text)
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(5), default="RIGHT")
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    replacement_code: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="accepted")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    pass_name: Mapped[str] = mapped_column(String(32), default="general")
    context_refs: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created_at()
