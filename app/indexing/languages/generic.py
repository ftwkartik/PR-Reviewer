"""Chunkers for non-code files: markdown (by heading), YAML/TOML (by top-level key), and
line-window fallback for everything else. Config and docs feed Tier 4/5 retrieval."""

import re

from app.domain.code import CodeChunk
from app.github.classify import is_test_path

WINDOW_LINES = 80
_MD_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_TOP_KEY = re.compile(r"^(?:\[\[?([^\]]+)\]\]?|([A-Za-z_][\w.-]*)\s*[:=])")


def _section_chunks(
    path: str, language: str, lines: list[str], starts: list[tuple[int, str]], symbol_type: str
) -> list[CodeChunk]:
    out: list[CodeChunk] = []
    if not starts or starts[0][0] > 0:
        starts = [(0, "<preamble>"), *starts]
    for idx, (start, title) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        block = lines[start:end]
        while block and not block[-1].strip():
            block = block[:-1]
        if not "".join(block).strip():
            continue
        for off in range(0, len(block), WINDOW_LINES):
            part = block[off : off + WINDOW_LINES]
            out.append(
                CodeChunk(
                    path=path,
                    language=language,
                    symbol_type=symbol_type,
                    start_line=start + off + 1,
                    end_line=start + off + len(part),
                    content="\n".join(part),
                    symbol=title,
                    qualified_name=title,
                    is_test=is_test_path(path),
                )  # fmt: skip
            )
    return out


class GenericAdapter:
    language = "generic"

    def __init__(self, language: str) -> None:
        self.language = language

    def chunk(self, path: str, source: str) -> list[CodeChunk]:
        lines = source.splitlines()
        if self.language == "markdown":
            starts = [(i, m[2]) for i, ln in enumerate(lines) if (m := _MD_HEADING.match(ln))]
            return _section_chunks(path, "markdown", lines, starts, "doc")
        if self.language in {"yaml", "toml", "ini"}:
            starts = []
            for i, ln in enumerate(lines):
                m = _TOP_KEY.match(ln)
                if not m or ln.startswith((" ", "\t", "#")):
                    continue
                # TOML/INI keys at column 0 belong to the preceding [table]; only tables split.
                if self.language != "yaml" and not m[1]:
                    continue
                starts.append((i, (m[1] or m[2]).strip()))
            return _section_chunks(path, self.language, lines, starts, "config")
        kind = "config" if self.language in {"json", "dockerfile", "make", "sql"} else "fallback"
        return _section_chunks(path, self.language, lines, [], kind)
