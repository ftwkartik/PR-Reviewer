from app.github.pr_builder import build_changed_file
from app.review.batching import context_budget, make_batches, split_by_hunks
from app.review.diff_render import render_file_diff


def file(path: str, hunks: int = 1, lines_per: int = 10):  # type: ignore[no-untyped-def]
    patch = []
    for h in range(hunks):
        start = 1 + h * 100
        patch.append(f"@@ -{start},0 +{start},{lines_per} @@")
        patch += [f"+line {h}-{i} " + "x" * 40 for i in range(lines_per)]
    return build_changed_file({"filename": path, "status": "modified", "additions": hunks * lines_per,
                               "deletions": 0, "patch": "\n".join(patch)})  # fmt: skip


def test_render_uses_new_line_numbers_and_marks_deletions() -> None:
    f = build_changed_file({"filename": "a.py", "status": "modified", "additions": 1, "deletions": 1,
                            "patch": "@@ -5,3 +5,3 @@\n keep\n-old\n+new\n tail"})  # fmt: skip
    out = render_file_diff(f)
    assert "     5   " in out and "     6 +     | new" in out and "-6    | old" in out


def test_small_files_share_a_batch() -> None:
    batches, skipped = make_batches([file("a.py"), file("b.py")], 24_000, 12)
    assert len(batches) == 1 and len(batches[0].files) == 2 and not skipped


def test_large_file_split_by_hunks() -> None:
    f = file("big.py", hunks=10, lines_per=40)
    parts = split_by_hunks(f, max_tokens=600)
    assert len(parts) > 1
    assert sum(len(p.hunks) for p in parts) == 10  # no hunk lost or duplicated
    assert [h.new_start for p in parts for h in p.hunks] == [h.new_start for h in f.hunks]


def test_model_call_limit_reserves_synthesis_call_and_reports_skips() -> None:
    files = [file(f"f{i}.py", hunks=4, lines_per=40) for i in range(6)]
    batches, skipped = make_batches(files, 4000, max_model_calls=3)
    assert len(batches) == 2  # 3 calls - 1 reserved for synthesis
    assert skipped and all(reason == "model_call_limit" for _, reason in skipped)


def test_context_budget_floor_scales_with_small_windows() -> None:
    batches, _ = make_batches([file("a.py")], 24_000, 12)
    assert context_budget(batches[0], 24_000) > 10_000
    assert context_budget(batches[0], 8_000) >= 2000  # normal windows keep the 2000-token floor
    # a tiny window must not be overrun by a flat floor (small local models): floor = window // 4
    assert context_budget(batches[0], 100) == 25
