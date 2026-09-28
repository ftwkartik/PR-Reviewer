"""A GitHub stand-in backed by in-memory file trees, so the real indexer/retriever/pipeline can
be exercised offline: base tree = the PR's base commit, head tree = the PR's head commit."""

import io
import tarfile
from pathlib import Path
from typing import Any

BASE_SHA = "b" * 40
HEAD_SHA = "a" * 40


class LocalGH:
    def __init__(self, base: dict[str, str], head: dict[str, str]) -> None:
        self.base, self.head = base, head
        self.calls = 0

    async def download_tarball(self, owner: str, repo: str, sha: str, dest: Path, **_: Any) -> Path:
        files = self.base if sha == BASE_SHA else self.head
        with tarfile.open(dest, "w:gz") as tar:
            for path, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(f"{owner}-{repo}-{sha[:7]}/{path}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return dest

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> bytes | None:
        files = self.head if ref == HEAD_SHA else self.base
        text = files.get(path)
        return text.encode() if text is not None else None

    async def compare(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise NotImplementedError  # evals always build the base snapshot in full
