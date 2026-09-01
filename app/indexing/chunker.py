"""Dispatch from language to chunker. Adding a language = one adapter + one registry entry."""

from app.domain.code import CodeChunk
from app.indexing.languages.base import LanguageAdapter
from app.indexing.languages.generic import GenericAdapter
from app.indexing.languages.python import PythonAdapter

_ADAPTERS: dict[str, LanguageAdapter] = {"python": PythonAdapter()}


def adapter_for(language: str) -> LanguageAdapter:
    return _ADAPTERS.get(language) or GenericAdapter(language)


def chunk_file(path: str, language: str, source: str, blob_sha: str) -> list[CodeChunk]:
    chunks = adapter_for(language).chunk(path, source)
    for c in chunks:
        c.blob_sha = blob_sha
    return chunks
