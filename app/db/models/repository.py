import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, created_at, uuid_pk

EMBEDDING_DIM = 1024


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    private: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created_at()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha", "embedding_model", "chunker_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    parent_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="building")  # building|ready|failed
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str] = mapped_column(String(128))
    chunker_version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class SnapshotFile(Base):
    """Manifest entry: a snapshot is a path -> blob_sha map, not a copy of chunks."""

    __tablename__ = "snapshot_files"
    __table_args__ = (Index("ix_snapshot_files_blob_sha", "blob_sha"),)

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_sha: Mapped[str] = mapped_column(String(40))
    language: Mapped[str | None] = mapped_column(String(32))


class CodeChunk(Base):
    """Content-addressed chunk, shared by every snapshot containing the same blob."""

    __tablename__ = "code_chunks"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "blob_sha",
            "chunker_version",
            "start_line",
            "symbol_type",
            "qualified_name",
            name="uq_code_chunks_identity",
        ),  # fmt: skip
        Index("ix_code_chunks_repo_blob", "repository_id", "blob_sha"),
        Index("ix_code_chunks_fts", "fts", postgresql_using="gin"),
        Index(
            "ix_code_chunks_qname_trgm",
            "qualified_name",
            postgresql_using="gin",
            postgresql_ops={"qualified_name": "gin_trgm_ops"},
        ),  # fmt: skip
        Index("ix_code_chunks_called_names", "called_names", postgresql_using="gin"),
        Index("ix_code_chunks_imports", "imports", postgresql_using="gin"),
        Index(
            "ix_code_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),  # fmt: skip
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    blob_sha: Mapped[str] = mapped_column(String(40))
    chunker_version: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(Text)
    qualified_name: Mapped[str | None] = mapped_column(Text)
    symbol_type: Mapped[str] = mapped_column(String(32))
    parent_symbol: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str | None] = mapped_column(Text)
    imports: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    called_names: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    bases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    fts: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(qualified_name,'') || ' ' || "
            "coalesce(signature,'') || ' ' || content)",
            persisted=True,
        ),
    )


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(128), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
