from app.core.config import Settings
from app.github.pr_builder import build_changed_file


def raw(**kw):  # type: ignore[no-untyped-def]
    base = {"filename": "app/auth/session.py", "status": "modified", "additions": 1, "deletions": 1,
            "patch": "@@ -1,2 +1,2 @@\n a\n-b\n+c", "sha": "abc"}  # fmt: skip
    return {**base, **kw}


def test_modified_python_file() -> None:
    f = build_changed_file(raw())
    assert f.language == "python" and not f.skip_reason and f.added_lines() == {2}
    assert [(r.start, r.end) for r in f.changed_line_ranges] == [(2, 2)]


def test_test_file_flag() -> None:
    assert build_changed_file(raw(filename="tests/test_x.py")).is_test


def test_binary_lockfile_generated_skipped() -> None:
    assert build_changed_file(raw(filename="logo.png", patch=None)).skip_reason == "binary"
    assert build_changed_file(raw(filename="uv.lock")).skip_reason == "lockfile"
    assert build_changed_file(raw(filename="dist/app.min.js")).skip_reason == "generated"
    assert build_changed_file(raw(filename="api/x_pb2.py")).skip_reason == "generated"


def test_missing_patch_marked() -> None:
    assert build_changed_file(raw(patch=None)).skip_reason == "no_patch"


def test_malformed_patch_skips_file_not_review() -> None:
    f = build_changed_file(raw(patch="not a patch"))
    assert f.skip_reason == "malformed_patch" and not f.hunks


def test_oversized_patch() -> None:
    s = Settings(_env_file=None, max_patch_size_bytes=10)  # type: ignore[call-arg]
    assert build_changed_file(raw(), s).skip_reason == "patch_too_large"


def test_rename_keeps_previous_path() -> None:
    f = build_changed_file(raw(status="renamed", previous_filename="old.py", patch=None,
                               additions=0, deletions=0))  # fmt: skip
    assert f.previous_path == "old.py" and f.skip_reason == "rename_only"
