"""Randomized check: parsed line numbers must point at the right text in the new/old file."""

import difflib
import random

from app.github.diff_parser import parse_patch


def make_patch(old: list[str], new: list[str]) -> str:
    diff = list(difflib.unified_diff(old, new, lineterm="", n=3))
    return "\n".join(diff[2:])  # drop ---/+++ like GitHub's per-file patch


def test_line_numbers_map_to_real_text() -> None:
    rng = random.Random(1234)
    vocab = [f"line{i}" for i in range(30)] + ["", "    indented", "x = 1"]
    checked = 0
    for _ in range(300):
        old = [rng.choice(vocab) for _ in range(rng.randint(1, 40))]
        new = list(old)
        for _ in range(rng.randint(1, 6)):
            op = rng.choice(["ins", "del", "mod"])
            i = rng.randrange(len(new) + 1)
            if op == "ins":
                new.insert(i, rng.choice(vocab))
            elif new:
                i = min(i, len(new) - 1)
                if op == "del":
                    del new[i]
                else:
                    new[i] = rng.choice(vocab) + "!"
        if old == new:
            continue
        for h in parse_patch(make_patch(old, new)):
            for ln in h.lines:
                if ln.new_line is not None:
                    assert new[ln.new_line - 1] == ln.text
                    checked += 1
                if ln.old_line is not None:
                    assert old[ln.old_line - 1] == ln.text
                    checked += 1
    assert checked > 1000
