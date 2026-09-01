import hashlib
from dataclasses import dataclass, field

from app.retrieval.tokens import estimate_tokens

CHUNKER_VERSION = "1"
MAX_EMBED_CHARS = 6000


@dataclass(slots=True)
class CodeChunk:
    path: str
    language: str | None
    symbol_type: str  # module | class | function | method | config | doc | fallback
    start_line: int
    end_line: int
    content: str
    symbol: str | None = None
    qualified_name: str | None = None  # e.g. "SessionManager.verify_token"
    parent_symbol: str | None = None
    signature: str | None = None
    imports: list[str] = field(default_factory=list)
    called_names: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    is_test: bool = False
    blob_sha: str = ""
    docstring: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)

    def embedding_text(self) -> str:
        """Header-enriched text that is embedded; stored `content` stays verbatim."""
        head = [f"# {self.path}"]
        if self.qualified_name:
            head.append(f"# {self.symbol_type} {self.qualified_name}")
        if self.signature:
            head.append(self.signature)
        if self.docstring:
            head.append(self.docstring[:400])
        return ("\n".join(head) + "\n" + self.content)[:MAX_EMBED_CHARS]
