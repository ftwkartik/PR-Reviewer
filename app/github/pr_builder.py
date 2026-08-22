"""Turn GitHub REST payloads into domain objects."""

from typing import Any

import structlog

from app.core.config import Settings
from app.domain.pr import ChangedFile, PullRequestContext
from app.github import classify
from app.github.diff_parser import DiffParseError, changed_line_ranges, parse_patch

log = structlog.get_logger()


def build_changed_file(raw: dict[str, Any], settings: Settings | None = None) -> ChangedFile:
    path = raw["filename"]
    patch = raw.get("patch")
    f = ChangedFile(
        path=path,
        status=raw.get("status", "modified"),
        additions=int(raw.get("additions", 0)),
        deletions=int(raw.get("deletions", 0)),
        patch=patch,
        previous_path=raw.get("previous_filename"),
        blob_sha=raw.get("sha"),
        language=classify.detect_language(path),
        is_test=classify.is_test_path(path),
        is_generated=classify.is_generated_path(path),
        is_binary=classify.is_binary_path(path),
        is_lockfile=classify.is_lockfile(path),
    )
    if f.is_binary:
        f.skip_reason = "binary"
    elif f.is_lockfile:
        f.skip_reason = "lockfile"
    elif f.is_generated:
        f.skip_reason = "generated"
    elif patch is None:
        # GitHub omits `patch` for binary files and for diffs it considers too large.
        f.skip_reason = "no_patch" if f.status != "renamed" or f.changed_lines else "rename_only"
    elif settings and len(patch.encode()) > settings.max_patch_size_bytes:
        f.skip_reason = "patch_too_large"
    if f.skip_reason is None and patch is not None:
        try:
            f.hunks = parse_patch(patch)
            f.changed_line_ranges = changed_line_ranges(f.hunks)
        except DiffParseError as exc:
            log.warning("patch_parse_failed", path=path, error=str(exc))
            f.skip_reason = "malformed_patch"
    return f


def build_pr_context(
    pr: dict[str, Any], files: list[dict[str, Any]], settings: Settings | None = None
) -> PullRequestContext:
    return PullRequestContext(
        repo_full_name=pr["base"]["repo"]["full_name"],
        number=pr["number"],
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        base_sha=pr["base"]["sha"],
        head_sha=pr["head"]["sha"],
        author=(pr.get("user") or {}).get("login", "unknown"),
        installation_id=None,
        files=[build_changed_file(f, settings) for f in files],
        total_files_reported=int(pr.get("changed_files", len(files))),
    )
