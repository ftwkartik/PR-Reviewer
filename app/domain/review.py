"""Review domain: the LLM-facing schema (strict) and the application-side finding record."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal[
    "correctness", "security", "performance", "concurrency", "database",
    "api", "maintainability", "testing", "compatibility",
]  # fmt: skip
Risk = Literal["none", "low", "medium", "high", "critical"]
Side = Literal["RIGHT", "LEFT"]

SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}
MAX_SPAN_LINES = 30
MAX_FINDINGS_PER_CALL = 25


class ReviewFinding(BaseModel):
    """One finding as proposed by the model. Everything here is UNTRUSTED until validated."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path of the changed file, exactly as shown in the diff header")
    line_start: int = Field(
        ge=1, description="First line, using the line numbers printed in the diff"
    )
    line_end: int = Field(ge=1, description="Last line (same as line_start for a single line)")
    side: Side = Field(
        default="RIGHT", description="RIGHT for added/context lines, LEFT for deleted"
    )
    severity: Severity
    category: Category
    title: str = Field(
        min_length=8, max_length=120, description="Specific, one line, no trailing period"
    )
    explanation: str = Field(
        min_length=20,
        description="What is wrong, why it is wrong, and what can happen as a result",
    )
    evidence_quote: str = Field(
        min_length=3,
        description="Verbatim code copied from the diff or context that supports the claim",
    )
    context_refs: list[str] = Field(
        default_factory=list, description="ids of <context_item> blocks relied upon, e.g. c3"
    )
    suggested_fix: str = Field(description="Concrete, actionable correction")
    replacement_code: str | None = Field(
        default=None,
        description="Exact replacement for line_start..line_end; only if no unseen code is needed",
    )
    confidence: float = Field(
        ge=0, le=1, description="Probability this is a real, worthwhile problem"
    )

    @model_validator(mode="after")
    def _range_ok(self) -> "ReviewFinding":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        if self.line_end - self.line_start + 1 > MAX_SPAN_LINES:
            raise ValueError(f"finding spans more than {MAX_SPAN_LINES} lines")
        return self


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="2-4 sentences on what the change does and its main risk")
    overall_risk: Risk
    findings: list[ReviewFinding] = Field(max_length=MAX_FINDINGS_PER_CALL)
    positive_observations: list[str] = Field(
        default_factory=list, max_length=5, description="Brief notes on things done well"
    )


class SynthesisVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, description="Index of the finding in the provided list")
    keep: bool
    reason: str = Field(max_length=300)


class SynthesisResult(BaseModel):
    """PR-level pass: may only keep/drop existing findings and add summary-level notes."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    verdicts: list[SynthesisVerdict]
    cross_file_observations: list[str] = Field(default_factory=list, max_length=5)


FindingStatus = Literal[
    "accepted", "rejected", "below_threshold", "duplicate", "low_severity", "capped",
    "published", "publish_failed",
]  # fmt: skip


@dataclass
class ProcessedFinding:
    """A model finding plus the application's decision about it."""

    finding: ReviewFinding
    batch_index: int
    status: FindingStatus = "accepted"
    reject_reason: str | None = None
    fingerprint: str = ""
    pass_name: str = "general"  # noqa: S105 - name of the review pass, not a credential
    confidence: float = 0.0  # possibly downgraded by validation
    replacement_code: str | None = None  # None if the suggestion failed validation
    notes: list[str] = field(default_factory=list)
    original: dict[str, object] = field(default_factory=dict)  # model output before validation

    def __post_init__(self) -> None:
        self.original = self.finding.model_dump()
        if not self.confidence:
            self.confidence = self.finding.confidence
        if self.replacement_code is None:
            self.replacement_code = self.finding.replacement_code

    @property
    def rank(self) -> float:
        return SEVERITY_RANK[self.finding.severity] * 10 + self.confidence
