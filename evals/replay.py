"""Simulated reviewers for exercising the scoring + validation pipeline WITHOUT a real model.

These are NOT language models and their scores say nothing about review quality. They exist to
prove the harness measures what it claims to: a perfect reviewer scores 1.0, a hallucinating one is
stopped by the validator, and a reviewer that emits plausible-but-wrong anchored comments is
penalised by precision/false-positive rate.
"""

from typing import Any, Literal

from pydantic import BaseModel

from app.domain.review import ReviewFinding, ReviewResult, SynthesisResult, SynthesisVerdict
from app.llm.base import LLMRequest
from evals.cases import BenchmarkCase

Style = Literal["perfect", "noisy", "sloppy"]


class SimulatedReviewer:
    def __init__(self, cases: list[BenchmarkCase], style: Style) -> None:
        self._cases, self._style = cases, style

    def _case_for(self, req: LLMRequest[Any]) -> BenchmarkCase:
        for c in self._cases:
            if f"Title: {c.title}" in req.user:
                return c
        raise LookupError("no benchmark case matches this prompt")

    def __call__(self, req: LLMRequest[Any]) -> BaseModel:
        if req.purpose == "synthesis":
            return SynthesisResult(
                summary="Simulated summary.",
                verdicts=[
                    SynthesisVerdict(index=i, keep=True, reason="simulated") for i in range(25)
                ],
            )
        case = self._case_for(req)
        findings: list[ReviewFinding] = []
        for e in case.expected:
            title = "Seeded defect in the changed code"
            findings.append(
                _finding(
                    e.file, e.line_start, e.contains, e.categories[0], e.min_severity, title, 0.92
                )
            )
        if self._style in {"noisy", "sloppy"}:
            findings += _hallucinations(case)
        if self._style == "sloppy":
            for f in case.changed:
                if f.added_lines():
                    line = min(f.added_lines())
                    text = next(
                        ln.text for h in f.hunks for ln in h.lines if ln.new_line == line
                    ).strip()
                    if len(text) >= 6:
                        title = "Consider restructuring this block"
                        findings.append(
                            _finding(f.path, line, text, "maintainability", "medium", title, 0.9)
                        )
        risk = "medium" if findings else "none"
        return ReviewResult(summary="Simulated review.", overall_risk=risk, findings=findings[:25])


def _finding(
    path: str, line: int, quote: str, category: str, severity: str, title: str, conf: float
) -> ReviewFinding:
    return ReviewFinding.model_validate({
        "path": path, "line_start": line, "line_end": line, "severity": severity,
        "category": category, "title": f"{title} ({path.split('/')[-1]}:{line})",
        "confidence": conf, "evidence_quote": quote,
        "explanation": "The changed code has a concrete defect that can cause incorrect behaviour.",
        "suggested_fix": "Correct the logic as described above.",
    })  # fmt: skip


def _hallucinations(case: BenchmarkCase) -> list[ReviewFinding]:
    some = case.changed[0]
    return [
        _finding(
            "app/does_not_exist.py", 5, "def ghost():", "correctness", "high", "Invented file", 0.95
        ),
        _finding(
            some.path,
            min(some.right_lines() or {1}),
            "os.system(user_supplied_command)",
            "security",
            "critical",
            "Fabricated evidence",
            0.97,
        ),  # fmt: skip
        _finding(
            some.path, 9999, "anything at all", "correctness", "high", "Line outside the diff", 0.9
        ),
        _finding(
            some.path,
            min(some.right_lines() or {1}),
            "x" * 8,
            "correctness",
            "low",
            "Low confidence guess",
            0.2,
        ),
    ]
