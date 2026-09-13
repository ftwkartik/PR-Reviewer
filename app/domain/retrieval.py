from dataclasses import dataclass, field
from enum import IntEnum

from app.domain.code import CodeChunk


class Tier(IntEnum):
    """Priority classes for context (lower = more important). See the planning doc, section 12."""

    IMMEDIATE = 1  # changed symbol, containing class (head version)
    DEPENDENCY = 2  # imported / called definitions
    USAGE = 3  # callers of the changed symbols
    VALIDATION = 4  # tests, config, migrations
    RAG = 5  # hybrid search extras
    CONVENTIONS = 6  # README / CONTRIBUTING / docs

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(slots=True)
class RetrievedContext:
    chunk: CodeChunk
    tier: Tier
    score: float
    reasons: list[str] = field(default_factory=list)
    ctx_id: str = ""  # stable id the model must cite, e.g. "c3"

    @property
    def key(self) -> str:
        c = self.chunk
        return f"{c.origin}:{c.path}:{c.start_line}:{c.end_line}"


@dataclass(slots=True)
class DropInfo:
    path: str
    lines: str
    tier: Tier
    reason: str  # budget | duplicate | contained


@dataclass(slots=True)
class ContextBundle:
    items: list[RetrievedContext] = field(default_factory=list)
    tokens_used: int = 0
    budget: int = 0
    dropped: list[DropInfo] = field(default_factory=list)

    def by_id(self) -> dict[str, RetrievedContext]:
        return {i.ctx_id: i for i in self.items}

    def paths(self) -> list[str]:
        seen: dict[str, None] = {}
        for i in self.items:
            seen[i.chunk.path] = None
        return list(seen)
