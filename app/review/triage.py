"""Cheap, deterministic large-PR triage. No LLM involved.

Decides which changed files are reviewed and in what priority, and records an explicit
reason for every file that is not, so the final summary never truncates silently.
"""

import re
from dataclasses import dataclass, field

from app.core.config import Settings
from app.domain.pr import ChangedFile

_SENSITIVE = re.compile(
    r"auth|login|session|token|password|secret|crypt|permission|acl|payment|migrat|security|sql|db|database",
    re.I,
)


@dataclass
class TriageResult:
    selected: list[ChangedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    total_files: int = 0
    total_changed_lines: int = 0
    reviewed_changed_lines: int = 0
    degraded: bool = False

    def as_scope(self) -> dict[str, object]:
        return {
            "total_files": self.total_files,
            "reviewed_files": len(self.selected),
            "total_changed_lines": self.total_changed_lines,
            "reviewed_changed_lines": self.reviewed_changed_lines,
            "skipped": [{"path": p, "reason": r} for p, r in self.skipped],
            "degraded": self.degraded,
        }


def priority(f: ChangedFile) -> float:
    score = 0.0
    if f.language == "python" and not f.is_test:
        score += 100
    elif (
        f.language in {"javascript", "typescript", "go", "java", "ruby", "rust", "sql"}
        and not f.is_test
    ):
        score += 80
    elif f.is_test:
        score += 40
    elif f.language in {"yaml", "toml", "json", "ini", "shell"}:
        score += 30
    else:
        score += 10
    if _SENSITIVE.search(f.path):
        score += 25
    if f.status == "added":
        score += 5
    score += min(f.changed_lines, 200) / 20  # favour substantive change, cap outliers
    return score


def triage(files: list[ChangedFile], settings: Settings) -> TriageResult:
    res = TriageResult(
        total_files=len(files), total_changed_lines=sum(f.changed_lines for f in files)
    )
    candidates: list[ChangedFile] = []
    for f in files:
        if f.skip_reason:
            res.skipped.append((f.path, f.skip_reason))
        elif f.status == "removed" and not f.hunks:
            res.skipped.append((f.path, "deleted_file"))
        elif f.language == "markdown" and not f.path.lower().startswith(("docs/", "readme")):
            res.skipped.append((f.path, "documentation"))
        else:
            candidates.append(f)
    candidates.sort(key=priority, reverse=True)
    lines = 0
    for f in candidates:
        if len(res.selected) >= settings.max_files_per_review:
            res.skipped.append((f.path, "file_limit"))
            res.degraded = True
        elif lines + f.changed_lines > settings.max_changed_lines and res.selected:
            res.skipped.append((f.path, "changed_lines_limit"))
            res.degraded = True
        else:
            res.selected.append(f)
            lines += f.changed_lines
    res.reviewed_changed_lines = lines
    return res
