import difflib

from app.domain.pr import ChangedFile
from app.github.pr_builder import build_changed_file


def make_patch(old: str, new: str) -> str:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3))
    return "\n".join(diff[2:])


def changed_file(path: str, old: str, new: str, status: str = "modified") -> ChangedFile:
    adds = sum(1 for ln in difflib.ndiff(old.splitlines(), new.splitlines()) if ln.startswith("+ "))
    dels = sum(1 for ln in difflib.ndiff(old.splitlines(), new.splitlines()) if ln.startswith("- "))
    return build_changed_file(
        {
            "filename": path,
            "status": status,
            "additions": adds,
            "deletions": dels,
            "patch": make_patch(old, new),
        }  # fmt: skip
    )
