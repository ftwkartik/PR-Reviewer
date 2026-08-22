from app.core.config import Settings
from app.github.pr_builder import build_changed_file
from app.review.triage import triage


def f(path: str, adds: int = 5, status: str = "modified"):  # type: ignore[no-untyped-def]
    patch = f"@@ -1,0 +1,{adds} @@\n" + "\n".join("+x" for _ in range(adds))
    return build_changed_file({"filename": path, "status": status, "additions": adds,
                               "deletions": 0, "patch": patch})  # fmt: skip


def settings(**kw):  # type: ignore[no-untyped-def]
    return Settings(_env_file=None, **kw)


def test_prioritizes_source_over_tests_and_config() -> None:
    res = triage([f("README.txt"), f("tests/test_a.py"), f("app/a.py"), f("cfg.yaml")], settings())
    assert [x.path for x in res.selected][:2] == ["app/a.py", "tests/test_a.py"]


def test_sensitive_paths_boosted() -> None:
    res = triage([f("app/util.py"), f("app/auth/session.py")], settings())
    assert res.selected[0].path == "app/auth/session.py"


def test_file_limit_is_explicit_not_silent() -> None:
    files = [f(f"app/m{i}.py") for i in range(5)]
    res = triage(files, settings(max_files_per_review=2))
    assert len(res.selected) == 2 and res.degraded
    assert {r for _, r in res.skipped} == {"file_limit"}
    assert res.as_scope()["reviewed_files"] == 2 and res.as_scope()["total_files"] == 5


def test_changed_lines_limit() -> None:
    res = triage([f("app/a.py", 80), f("app/b.py", 80)], settings(max_changed_lines=100))
    assert (
        len(res.selected) == 1
        and ("app/b.py", "changed_lines_limit") in res.skipped
        or (
            "app/a.py",
            "changed_lines_limit",
        )
        in res.skipped
    )


def test_single_huge_file_still_reviewed() -> None:
    res = triage([f("app/big.py", 500)], settings(max_changed_lines=100))
    assert len(res.selected) == 1


def test_skipped_reasons_recorded() -> None:
    res = triage([f("uv.lock"), f("app/a.py")], settings())
    assert ("uv.lock", "lockfile") in res.skipped
