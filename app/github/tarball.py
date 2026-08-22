"""Safe extraction of untrusted repository archives.

Rejects absolute paths, `..` traversal, symlinks/hardlinks/devices, and enforces caps on
file count and total uncompressed size (zip/tar bomb defense). Nothing is ever executed.
"""

import tarfile
from pathlib import Path, PurePosixPath

from app.core.errors import PermanentError


def safe_extract(
    archive: Path, dest: Path, *, max_files: int = 50_000, max_total_bytes: int = 1024**3,
    max_file_bytes: int = 5 * 1024**2,
) -> list[str]:  # fmt: skip
    """Extract regular files from a GitHub tarball into `dest`, stripping the top-level dir.

    Returns relative POSIX paths extracted. Oversized individual files are skipped (they are
    not indexable anyway); everything else that violates policy aborts extraction.
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    total = 0
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isreg():
                if member.issym() or member.islnk():
                    continue  # never materialize links
                raise PermanentError(
                    f"unsafe archive member type: {member.name!r}", code="unsafe_archive"
                )
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2:
                continue
            rel = PurePosixPath(*parts[1:])  # strip "owner-repo-sha/"
            if rel.is_absolute() or ".." in rel.parts:
                raise PermanentError(
                    f"path traversal in archive: {member.name!r}", code="unsafe_archive"
                )
            if member.size > max_file_bytes:
                continue
            total += member.size
            if total > max_total_bytes or len(extracted) >= max_files:
                raise PermanentError("archive exceeds extraction limits", code="repo_too_large")
            target = (dest / rel).resolve()
            if not target.is_relative_to(dest):
                raise PermanentError(
                    f"path escapes destination: {member.name!r}", code="unsafe_archive"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, target.open("wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
            extracted.append(rel.as_posix())
    return extracted
