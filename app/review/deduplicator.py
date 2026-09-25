"""Deterministic deduplication and comment caps. No model calls."""

import re
from collections import defaultdict

from app.domain.review import ProcessedFinding

ADJACENT_LINES = 3
SIMILARITY = 0.6


def _tokens(pf: ProcessedFinding) -> set[str]:
    text = f"{pf.finding.title} {pf.finding.explanation}".lower()
    return {t for t in re.findall(r"[a-z0-9_]{3,}", text)}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _near(a: ProcessedFinding, b: ProcessedFinding) -> bool:
    fa, fb = a.finding, b.finding
    return (
        fa.line_start <= fb.line_end + ADJACENT_LINES
        and fb.line_start <= fa.line_end + ADJACENT_LINES
    )


def same_issue(a: ProcessedFinding, b: ProcessedFinding) -> bool:
    if a.finding.path != b.finding.path or not _near(a, b):
        return False
    if a.finding.category == b.finding.category:
        return True
    return jaccard(_tokens(a), _tokens(b)) >= SIMILARITY


def deduplicate(findings: list[ProcessedFinding]) -> None:
    """Mark lower-ranked duplicates in place. Only considers still-accepted findings."""
    live = sorted((f for f in findings if f.status == "accepted"), key=lambda f: -f.rank)
    kept: list[ProcessedFinding] = []
    for f in live:
        match = next((k for k in kept if same_issue(k, f)), None)
        if match is None:
            kept.append(f)
            continue
        f.status, f.reject_reason = "duplicate", f"duplicate of {match.fingerprint}"
        match.confidence = max(match.confidence, f.confidence)  # independent agreement
        match.notes.append(f"also found by batch {f.batch_index}")


def apply_caps(findings: list[ProcessedFinding], per_file: int = 3, total: int = 10) -> None:
    live = sorted((f for f in findings if f.status == "accepted"), key=lambda f: -f.rank)
    per: dict[str, int] = defaultdict(int)
    n = 0
    for f in live:
        if n >= total or per[f.finding.path] >= per_file:
            f.status, f.reject_reason = "capped", "comment cap reached (lower priority)"
            continue
        per[f.finding.path] += 1
        n += 1
