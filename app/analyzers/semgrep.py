import json
import shutil
from pathlib import Path

from app.analyzers.base import AnalyzerUnavailableError, StaticFinding, run_limited

RULES = Path(__file__).parent / "rules" / "semgrep.yml"
_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


class SemgrepAnalyzer:
    """Optional: requires the `semgrep` binary. Uses only the vetted local ruleset, offline."""

    name = "semgrep"

    async def run(self, root: Path, paths: list[str], timeout_s: float) -> list[StaticFinding]:
        exe = shutil.which("semgrep")
        if exe is None:
            raise AnalyzerUnavailableError("semgrep")
        _, out = await run_limited(
            [
                exe,
                "--config",
                str(RULES),
                "--json",
                "--metrics=off",
                "--disable-version-check",
                "--no-git-ignore",
                "--quiet",
                "--timeout",
                "10",
                "--",
                *paths,
            ],
            root,
            timeout_s,
        )
        return parse_semgrep(out)


def parse_semgrep(raw: bytes) -> list[StaticFinding]:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return []
    out: list[StaticFinding] = []
    for it in data.get("results", []):
        extra = it.get("extra", {})
        rule = it.get("check_id", "semgrep").rsplit(".", 1)[-1]
        out.append(
            StaticFinding(
                "semgrep",
                rule,
                Path(it["path"]).as_posix(),
                int(it["start"]["line"]),
                _SEV.get(extra.get("severity", ""), "medium"),
                extra.get("message", ""),
            )
        )
    return out
