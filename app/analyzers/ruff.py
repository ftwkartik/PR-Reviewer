import json
import shutil
import sys
from pathlib import Path

from app.analyzers.base import AnalyzerUnavailableError, StaticFinding, run_limited

# Correctness-oriented rules only. Style/format rules are deliberately excluded: the point is
# evidence for reasoning, not lint noise.
SELECT = "F,B,ASYNC,PLE,E9,S102,S105,S106,S301,S307,S506,S602,S608"


class RuffAnalyzer:
    name = "ruff"

    async def run(self, root: Path, paths: list[str], timeout_s: float) -> list[StaticFinding]:
        py = [p for p in paths if p.endswith(".py")]
        if not py:
            return []
        exe = shutil.which("ruff", path=str(Path(sys.executable).parent)) or shutil.which("ruff")
        if exe is None:
            raise AnalyzerUnavailableError("ruff")
        code, out = await run_limited(
            [
                exe,
                "check",
                "--isolated",
                "--no-cache",
                "--output-format",
                "json",
                "--select",
                SELECT,
                "--",
                *py,
            ],
            root,
            timeout_s,
        )
        return parse_ruff(out, root)


def parse_ruff(raw: bytes, root: Path | None = None) -> list[StaticFinding]:
    try:
        items = json.loads(raw or b"[]")
    except json.JSONDecodeError:
        return []
    out: list[StaticFinding] = []
    for it in items:
        rule = it.get("code") or "ruff"
        sev = "high" if rule.startswith(("F82", "E9", "PLE")) else "medium"
        out.append(
            StaticFinding(
                "ruff",
                rule,
                _rel(it["filename"], root),
                int(it["location"]["row"]),
                sev,
                it.get("message", ""),
            )
        )
    return out


def _rel(path: str, root: Path | None) -> str:
    """ruff prints absolute paths; make them relative to the scratch root (the PR path)."""
    p = Path(path)
    if root is not None and p.is_absolute():
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return p.name
    return p.as_posix()
