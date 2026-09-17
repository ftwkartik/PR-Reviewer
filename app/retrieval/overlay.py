"""Head-version overlay.

The index holds the PR's *base* commit. For files the PR touches, the code the reviewer must
reason about is the *head* version, so those files are chunked in memory from the head SHA and
overlaid on the index (and the base versions of those paths are excluded from retrieval). The
overlay is never persisted or embedded.
"""

import asyncio
from collections.abc import Awaitable, Callable

from app.domain.code import CodeChunk
from app.domain.pr import ChangedFile
from app.indexing.chunker import chunk_file
from app.indexing.ignore import MAX_FILE_BYTES

FetchFn = Callable[[str], Awaitable[bytes | None]]

OVERLAY_LANGUAGES = {"python", "javascript", "typescript", "go", "java", "ruby", "rust", "sql",
                     "yaml", "toml", "ini", "json", "dockerfile", "markdown", "shell"}  # fmt: skip


async def build_overlay(
    files: list[ChangedFile], fetch_head: FetchFn, concurrency: int = 8
) -> dict[str, list[CodeChunk]]:
    """path -> head-version chunks, for every changed file that still exists at head."""
    sem = asyncio.Semaphore(concurrency)

    async def one(f: ChangedFile) -> tuple[str, list[CodeChunk]]:
        if f.status == "removed" or not f.language or f.language not in OVERLAY_LANGUAGES:
            return f.path, []
        async with sem:
            data = await fetch_head(f.path)
        if data is None or len(data) > MAX_FILE_BYTES or b"\x00" in data[:8192]:
            return f.path, []
        chunks = chunk_file(f.path, f.language, data.decode("utf-8", errors="replace"), "head")
        for i, c in enumerate(chunks):
            c.origin = "head"
            c.chunk_id = f"head:{f.path}:{i}"
        return f.path, chunks

    return dict(await asyncio.gather(*(one(f) for f in files)))


def containing_chunks(chunks: list[CodeChunk], lines: frozenset[int]) -> list[CodeChunk]:
    """Smallest chunk(s) enclosing each line, e.g. the method rather than its class header."""
    picked: dict[str, CodeChunk] = {}
    for ln in sorted(lines):
        best: CodeChunk | None = None
        for c in chunks:
            if c.start_line <= ln <= c.end_line and (
                best is None or (c.end_line - c.start_line) < (best.end_line - best.start_line)
            ):
                best = c
        if best is not None:
            picked[best.chunk_id] = best
    return list(picked.values())
