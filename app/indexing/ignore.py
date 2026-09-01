"""Which files are worth indexing. Hard defaults + the repository's own .gitignore."""

from pathlib import PurePosixPath

import pathspec

from app.github import classify

MAX_FILE_BYTES = 512 * 1024

DEFAULT_IGNORE_PATTERNS = [
    ".git/", "node_modules/", "venv/", ".venv/", "env/", "dist/", "build/", "coverage/",
    "htmlcov/", "__pycache__/", ".tox/", ".mypy_cache/", ".pytest_cache/", ".ruff_cache/",
    "vendor/", "third_party/", "site-packages/", "*.egg-info/", ".idea/", ".vscode/",
    "*.min.js", "*.min.css", "*.map", "*.lock", "*.snap", "*.pyc",
    ".env", ".env.*", "!.env.example",
]  # fmt: skip

_INDEXABLE_NAMES = {"Dockerfile", "Makefile"}


class IgnoreRules:
    def __init__(self, gitignore_text: str | None = None, extra: list[str] | None = None) -> None:
        patterns = list(DEFAULT_IGNORE_PATTERNS)
        if gitignore_text:
            patterns += gitignore_text.splitlines()
        patterns += extra or []
        self._spec = pathspec.PathSpec.from_lines("gitignore", patterns)

    def is_ignored(self, path: str) -> bool:
        return self._spec.match_file(path) or classify.is_generated_path(path)


def is_indexable_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        not classify.is_binary_path(path)
        and not classify.is_lockfile(path)
        and (classify.detect_language(path) is not None or p.name in _INDEXABLE_NAMES)
    )


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def should_index(path: str, data: bytes, rules: IgnoreRules) -> tuple[bool, str]:
    """(index?, reason-if-not)."""
    if rules.is_ignored(path):
        return False, "ignored"
    if not is_indexable_path(path):
        return False, "unsupported_type"
    if len(data) > MAX_FILE_BYTES:
        return False, "too_large"
    if looks_binary(data):
        return False, "binary"
    try:
        text = data[:600].decode("utf-8", errors="ignore")
    except Exception:  # pragma: no cover
        return False, "undecodable"
    if classify.has_generated_marker(text):
        return False, "generated"
    return True, ""
