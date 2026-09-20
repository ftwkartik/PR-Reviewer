"""Render a ChangedFile's hunks with explicit line numbers for the model.

Added and context lines carry their NEW-file line number (the one a RIGHT-side review comment
uses); deleted lines are marked `-` with their OLD line number in a separate column. The model is
told to cite these numbers, and the validator still re-checks every one against the parsed diff.
"""

from app.domain.pr import ChangedFile, DiffHunk


def render_hunk(h: DiffHunk) -> str:
    out = [h.header]
    for ln in h.lines:
        if ln.kind == "del":
            out.append(f"      -{ln.old_line:<5}| {ln.text}")
        else:
            mark = "+" if ln.kind == "add" else " "
            out.append(f"{ln.new_line:>6} {mark}     | {ln.text}")
    return "\n".join(out)


def render_file_diff(f: ChangedFile) -> str:
    head = f"FILE: {f.path} ({f.status}"
    if f.previous_path:
        head += f", renamed from {f.previous_path}"
    head += f", +{f.additions}/-{f.deletions})"
    return "\n".join([head, *(render_hunk(h) for h in f.hunks)])
