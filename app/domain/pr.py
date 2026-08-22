from dataclasses import dataclass, field
from typing import Literal

FileStatus = Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
LineKind = Literal["add", "del", "context"]


@dataclass(frozen=True, slots=True)
class ChangedLine:
    kind: LineKind
    text: str
    old_line: int | None  # None for additions
    new_line: int | None  # None for deletions


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: tuple[ChangedLine, ...]

    @property
    def added_lines(self) -> frozenset[int]:
        return frozenset(ln.new_line for ln in self.lines if ln.kind == "add" and ln.new_line)

    @property
    def right_lines(self) -> frozenset[int]:
        """New-file line numbers a RIGHT-side review comment may target (added + context)."""
        return frozenset(ln.new_line for ln in self.lines if ln.new_line is not None)

    @property
    def deleted_lines(self) -> frozenset[int]:
        """Old-file line numbers a LEFT-side comment may target."""
        return frozenset(ln.old_line for ln in self.lines if ln.kind == "del" and ln.old_line)

    def new_text(self) -> str:
        """Post-change text of the hunk (added + context lines)."""
        return "\n".join(ln.text for ln in self.lines if ln.kind != "del")


@dataclass(frozen=True, slots=True)
class LineRange:
    start: int
    end: int  # inclusive


@dataclass(slots=True)
class ChangedFile:
    path: str
    status: FileStatus
    additions: int
    deletions: int
    patch: str | None = None
    previous_path: str | None = None
    blob_sha: str | None = None
    hunks: list[DiffHunk] = field(default_factory=list)
    changed_line_ranges: list[LineRange] = field(default_factory=list)
    language: str | None = None
    is_test: bool = False
    is_generated: bool = False
    is_binary: bool = False
    is_lockfile: bool = False
    skip_reason: str | None = None  # set by triage/parser; file is excluded from review

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def right_lines(self) -> frozenset[int]:
        return (
            frozenset().union(*(h.right_lines for h in self.hunks)) if self.hunks else frozenset()
        )

    def added_lines(self) -> frozenset[int]:
        return (
            frozenset().union(*(h.added_lines for h in self.hunks)) if self.hunks else frozenset()
        )

    def deleted_lines(self) -> frozenset[int]:
        return (
            frozenset().union(*(h.deleted_lines for h in self.hunks)) if self.hunks else frozenset()
        )

    def hunk_containing(self, line: int, side: str = "RIGHT") -> DiffHunk | None:
        for h in self.hunks:
            if line in (h.right_lines if side == "RIGHT" else h.deleted_lines):
                return h
        return None


@dataclass(slots=True)
class PullRequestContext:
    repo_full_name: str
    number: int
    title: str  # untrusted
    body: str  # untrusted
    base_sha: str
    head_sha: str
    author: str
    installation_id: int | None
    files: list[ChangedFile] = field(default_factory=list)
    total_files_reported: int = 0

    @property
    def owner(self) -> str:
        return self.repo_full_name.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repo_full_name.split("/", 1)[1]
