"""Static analyzers: deterministic evidence for the reviewer.

Security model (the point of this module): analyzers NEVER execute PR code. They run as
separate processes over files copied into a scratch directory, with a minimal environment
(no secrets), no shell, a wall-clock timeout, CPU/memory rlimits and a capped output size.
Repository-supplied analyzer configuration is ignored; only vetted rules shipped with this
service are used. Findings are fed to the model as evidence and are never posted directly.
"""

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

import structlog

log = structlog.get_logger()

MAX_OUTPUT_BYTES = 2_000_000
CPU_SECONDS = 60
MEMORY_BYTES = 1024 * 1024 * 1024
MAX_FILE_WRITE = 64 * 1024 * 1024

# RLIMIT_DATA bounds heap growth. RLIMIT_AS and RLIMIT_NPROC are intentionally NOT used: an
# address-space cap crashes Rust tools (they reserve large virtual ranges), and NPROC is per-user,
# so it breaks whenever the worker user already runs many processes.
_LIMITED_EXEC = (
    "import os, resource, sys\n"
    f"resource.setrlimit(resource.RLIMIT_CPU, ({CPU_SECONDS}, {CPU_SECONDS}))\n"
    f"resource.setrlimit(resource.RLIMIT_DATA, ({MEMORY_BYTES}, {MEMORY_BYTES}))\n"
    f"resource.setrlimit(resource.RLIMIT_FSIZE, ({MAX_FILE_WRITE}, {MAX_FILE_WRITE}))\n"
    "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)


@dataclass(frozen=True)
class StaticFinding:
    tool: str
    rule_id: str
    path: str
    line: int
    severity: str  # low | medium | high
    message: str


class Analyzer(Protocol):
    name: str

    async def run(self, root: Path, paths: list[str], timeout_s: float) -> list[StaticFinding]: ...


class AnalyzerUnavailableError(Exception):
    pass


def safe_relative(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p.as_posix()


def write_files(root: Path, files: dict[str, str]) -> list[str]:
    """Copy (path -> text) into `root`, refusing anything that could escape it."""
    written: list[str] = []
    base = root.resolve()
    for path, text in files.items():
        rel = safe_relative(path)
        if rel is None:
            continue
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(rel)
    return written


async def run_limited(cmd: list[str], cwd: Path, timeout_s: float) -> tuple[int, bytes]:
    """Run a command under rlimits with a scrubbed environment. Returns (returncode, stdout)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "LC_ALL": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}  # fmt: skip  # no API keys, tokens or DB URLs
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _LIMITED_EXEC, *cmd,
        cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout[:MAX_OUTPUT_BYTES]


async def run_analyzers(
    analyzers: list[Analyzer],
    files: dict[str, str],
    commentable: dict[str, frozenset[int]],
    timeout_s: float = 30.0,
) -> list[StaticFinding]:
    """Run each analyzer over `files`; keep only findings on lines present in the diff.

    Failures degrade gracefully (a missing or slow analyzer must never fail a review).
    """
    if not files:
        return []
    out: list[StaticFinding] = []
    with tempfile.TemporaryDirectory(prefix="sa-") as tmp:
        root = Path(tmp)
        paths = write_files(root, files)
        results = await asyncio.gather(*(_safe_run(a, root, paths, timeout_s) for a in analyzers))
    for found in results:
        out += [f for f in found if f.line in commentable.get(f.path, frozenset())]
    out.sort(key=lambda f: (f.path, f.line, f.tool, f.rule_id))
    return out


async def _safe_run(
    a: Analyzer, root: Path, paths: list[str], timeout_s: float
) -> list[StaticFinding]:
    try:
        return await a.run(root, paths, timeout_s)
    except AnalyzerUnavailableError:
        log.info("analyzer_unavailable", analyzer=a.name)
    except TimeoutError:
        log.warning("analyzer_timeout", analyzer=a.name)
    except Exception:
        log.warning("analyzer_failed", analyzer=a.name, exc_info=True)
    return []
