"""Parse GitHub `patch` strings into hunks and line maps.

GitHub returns per-file unified-diff text *without* the ---/+++ headers. Review comments may
only target lines inside a hunk, so this module is the ground truth for "is this line part
of the PR?" and everything downstream (validator, publisher) relies on it.
"""

import re

from app.domain.pr import ChangedLine, DiffHunk, LineRange

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class DiffParseError(ValueError):
    pass


def parse_patch(patch: str) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
    header: re.Match[str] | None = None
    raw_header = ""
    lines: list[ChangedLine] = []
    old_no = new_no = 0

    def flush() -> None:
        if header is None:
            return
        old_start, old_len = int(header[1]), int(header[2] if header[2] is not None else 1)
        new_start, new_len = int(header[3]), int(header[4] if header[4] is not None else 1)
        hunk = DiffHunk(old_start, old_len, new_start, new_len, raw_header, tuple(lines))
        seen_old = sum(1 for ln in lines if ln.kind != "add")
        seen_new = sum(1 for ln in lines if ln.kind != "del")
        if seen_old != old_len or seen_new != new_len:
            raise DiffParseError(f"hunk line counts disagree with header: {raw_header!r}")
        hunks.append(hunk)

    for raw in patch.split("\n"):
        m = _HUNK_RE.match(raw)
        if m:
            flush()
            header, raw_header, lines = m, raw, []
            old_no, new_no = int(m[1]), int(m[3])
            continue
        if header is None:
            if raw.strip() == "":
                continue
            raise DiffParseError(f"content before first hunk header: {raw[:60]!r}")
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            lines.append(ChangedLine("add", raw[1:], None, new_no))
            new_no += 1
        elif raw.startswith("-"):
            lines.append(ChangedLine("del", raw[1:], old_no, None))
            old_no += 1
        elif raw.startswith(" ") or raw == "":
            # A bare empty string is a context line whose leading space was stripped, except
            # for the trailing newline artefact at the very end of the patch.
            lines.append(ChangedLine("context", raw[1:], old_no, new_no))
            old_no += 1
            new_no += 1
        else:
            raise DiffParseError(f"unexpected diff line: {raw[:60]!r}")
    _drop_trailing_blank(lines, header)
    flush()
    return hunks


def _drop_trailing_blank(lines: list[ChangedLine], header: re.Match[str] | None) -> None:
    """`patch.split('\\n')` yields a phantom empty context line if the patch ends in a newline."""
    if header is None or not lines:
        return
    old_len = int(header[2] if header[2] is not None else 1)
    new_len = int(header[4] if header[4] is not None else 1)
    seen_old = sum(1 for ln in lines if ln.kind != "add")
    seen_new = sum(1 for ln in lines if ln.kind != "del")
    last = lines[-1]
    if last.kind == "context" and last.text == "" and (seen_old > old_len or seen_new > new_len):
        lines.pop()


def changed_line_ranges(hunks: list[DiffHunk]) -> list[LineRange]:
    """Contiguous ranges of *added* new-file lines."""
    nums = sorted({n for h in hunks for n in h.added_lines})
    ranges: list[LineRange] = []
    for n in nums:
        if ranges and n == ranges[-1].end + 1:
            ranges[-1] = LineRange(ranges[-1].start, n)
        else:
            ranges.append(LineRange(n, n))
    return ranges
