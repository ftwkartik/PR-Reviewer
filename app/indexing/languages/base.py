from typing import Protocol

from app.domain.code import CodeChunk


class LanguageAdapter(Protocol):
    """Turns source text into semantic chunks. One adapter per language."""

    language: str

    def chunk(self, path: str, source: str) -> list[CodeChunk]: ...
