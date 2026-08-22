import pytest

from app.github.diff_parser import DiffParseError, changed_line_ranges, parse_patch

PATCH = """@@ -10,6 +10,7 @@ def foo():
     a = 1
     b = 2
-    c = 3
+    c = 4
+    d = 5
     return a
 
     e = 0
@@ -40,3 +41,3 @@ class X:
     x = 1
-    y = 2
+    y = 3
     z = 4"""


def test_hunks_and_line_numbers() -> None:
    hunks = parse_patch(PATCH)
    assert len(hunks) == 2
    h = hunks[0]
    assert (h.old_start, h.new_start, h.new_len) == (10, 10, 7)
    assert h.added_lines == {12, 13}
    assert h.deleted_lines == {12}
    assert {10, 11, 12, 13, 14, 15} <= h.right_lines
    assert hunks[1].added_lines == {42}


def test_changed_line_ranges_merge_contiguous() -> None:
    ranges = changed_line_ranges(parse_patch(PATCH))
    assert [(r.start, r.end) for r in ranges] == [(12, 13), (42, 42)]


def test_no_newline_marker_ignored() -> None:
    p = "@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file"
    (h,) = parse_patch(p)
    assert h.added_lines == {1} and h.deleted_lines == {1}


def test_single_line_hunk_header_without_counts() -> None:
    (h,) = parse_patch("@@ -0,0 +1 @@\n+hello")
    assert h.added_lines == {1}


def test_new_file_patch() -> None:
    (h,) = parse_patch("@@ -0,0 +1,2 @@\n+a\n+b")
    assert h.right_lines == {1, 2}


def test_trailing_newline_does_not_create_phantom_line() -> None:
    (h,) = parse_patch("@@ -1,2 +1,2 @@\n a\n-b\n+c\n")
    assert len(h.lines) == 3


@pytest.mark.parametrize(
    "bad",
    [
        "garbage before header\n@@ -1 +1 @@\n-a\n+b",
        "@@ -1,3 +1,3 @@\n a\n+b",
        "@@ -1 +1 @@\n?weird",
    ],
)
def test_malformed_patches_raise(bad: str) -> None:
    with pytest.raises(DiffParseError):
        parse_patch(bad)


def test_context_blank_line_preserved() -> None:
    (h,) = parse_patch("@@ -1,3 +1,3 @@\n a\n \n-b\n+c")
    assert h.lines[1].kind == "context" and h.lines[1].text == ""
