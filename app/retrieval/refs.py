"""Turning import strings into repository paths (pure)."""

from collections.abc import Iterable
from dataclasses import dataclass

SOURCE_ROOTS = ("", "src/", "lib/")


@dataclass(frozen=True)
class ImportTarget:
    imp: str  # original dotted import, e.g. app.auth.tokens.decode_token
    path: str  # repository file the module resolves to
    symbol: str  # remainder after the module ("" when the module itself is imported)
    alias: str  # last dotted segment: the name the code uses


def candidate_paths(imp: str) -> list[tuple[str, str]]:
    """(file path, remaining symbol) pairs for every module prefix of a dotted import."""
    parts = imp.split(".")
    out: list[tuple[str, str]] = []
    for i in range(len(parts), 0, -1):
        mod = "/".join(parts[:i])
        symbol = ".".join(parts[i:])
        for root in SOURCE_ROOTS:
            out.append((f"{root}{mod}.py", symbol))
            out.append((f"{root}{mod}/__init__.py", symbol))
    return out


def resolve_imports(imports: Iterable[str], existing: set[str]) -> list[ImportTarget]:
    """Resolve each import to the longest module prefix that exists in `existing`."""
    targets: list[ImportTarget] = []
    for imp in dict.fromkeys(imports):
        for path, symbol in candidate_paths(imp):
            if path in existing:
                targets.append(ImportTarget(imp, path, symbol, imp.rsplit(".", 1)[-1]))
                break
    return targets


def all_candidate_paths(imports: Iterable[str]) -> list[str]:
    return sorted({p for imp in imports for p, _ in candidate_paths(imp)})
