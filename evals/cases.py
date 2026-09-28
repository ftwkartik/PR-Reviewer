"""Benchmark case loading: a shared base repo + per-case edits -> real diffs with known answers."""

import ast
import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.domain.pr import ChangedFile
from app.github.pr_builder import build_changed_file

ROOT = Path(__file__).parent / "benchmarks"
BASE_DIR = ROOT / "_base"
CASES_DIR = ROOT / "cases"
IGNORED_PARTS = {"__pycache__", ".pytest_cache"}


@dataclass
class ExpectedFinding:
    file: str
    line_start: int
    line_end: int
    categories: list[str]
    min_severity: str
    required_context: list[str]
    contains: str = ""


@dataclass
class BenchmarkCase:
    id: str
    kind: str  # seeded | clean | decoy | injection
    title: str
    body: str
    base_files: dict[str, str]
    head_files: dict[str, str]
    changed: list[ChangedFile]
    expected: list[ExpectedFinding]
    forbidden_phrases: list[str] = field(default_factory=list)

    @property
    def changed_paths(self) -> set[str]:
        return {f.path for f in self.changed}


def load_base() -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(BASE_DIR.rglob("*")):
        if p.is_file() and not (set(p.parts) & IGNORED_PARTS):
            files[p.relative_to(BASE_DIR).as_posix()] = p.read_text()
    return files


def _apply_edits(text: str, edits: list[dict[str, str]], path: str, case_id: str) -> str:
    for e in edits:
        old, new = e["old"].rstrip("\n"), e["new"].rstrip("\n")
        if old not in text:
            raise ValueError(f"{case_id}: edit target not found in {path}: {old[:60]!r}")
        text = text.replace(old, new, 1)
    return text


def _diff_patch(old: str, new: str) -> str:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3))
    return "\n".join(diff[2:])


def _line_of(text: str, needle: str) -> int:
    for i, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return i
    raise ValueError(f"expected anchor {needle!r} not found in head file")


def load_case(path: Path, base: dict[str, str]) -> BenchmarkCase:
    spec: dict[str, Any] = yaml.safe_load(path.read_text())
    head = dict(base)
    for file, edits in (spec.get("edits") or {}).items():
        head[file] = _apply_edits(base[file], edits, file, spec["id"])
    for file, content in (spec.get("add_files") or {}).items():
        head[file] = content
    for file, content in head.items():
        if file.endswith(".py"):
            ast.parse(content, filename=f"{spec['id']}:{file}")  # cases must be valid Python

    changed: list[ChangedFile] = []
    for file in sorted(
        set(head) - set(base) | {f for f in head if f in base and head[f] != base[f]}
    ):
        old = base.get(file, "")
        adds = sum(
            1
            for d in difflib.ndiff(old.splitlines(), head[file].splitlines())
            if d.startswith("+ ")
        )
        dels = sum(
            1
            for d in difflib.ndiff(old.splitlines(), head[file].splitlines())
            if d.startswith("- ")
        )
        changed.append(build_changed_file({
            "filename": file, "status": "added" if file not in base else "modified",
            "additions": adds, "deletions": dels, "patch": _diff_patch(old, head[file]),
        }))  # fmt: skip

    expected: list[ExpectedFinding] = []
    for e in spec.get("expected") or []:
        line = _line_of(head[e["file"]], e["contains"])
        expected.append(ExpectedFinding(
            file=e["file"], line_start=line, line_end=line, categories=e["categories"],
            min_severity=e.get("min_severity", "medium"),
            required_context=e.get("required_context", []), contains=e["contains"],
        ))  # fmt: skip
    return BenchmarkCase(
        id=spec["id"], kind=spec["kind"], title=spec["title"], body=spec.get("body", ""),
        base_files=base, head_files=head, changed=changed, expected=expected,
        forbidden_phrases=[p.lower() for p in spec.get("forbidden_phrases", [])],
    )  # fmt: skip


def load_all() -> list[BenchmarkCase]:
    base = load_base()
    return [load_case(p, base) for p in sorted(CASES_DIR.glob("*.yaml"))]
