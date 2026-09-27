import json
import shutil
import sys
from pathlib import Path

from app.analyzers.base import AnalyzerUnavailableError, StaticFinding, run_limited
from app.github.classify import is_test_path

_SEV = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
_SKIP_LOW_CONFIDENCE = {"LOW"}


class BanditAnalyzer:
    name = "bandit"

    async def run(self, root: Path, paths: list[str], timeout_s: float) -> list[StaticFinding]:
        py = [p for p in paths if p.endswith(".py") and not is_test_path(p)]
        if not py:
            return []
        exe = shutil.which("bandit", path=str(Path(sys.executable).parent)) or shutil.which(
            "bandit"
        )
        if exe is None:
            raise AnalyzerUnavailableError("bandit")
        _, out = await run_limited([exe, "-q", "-f", "json", "--", *py], root, timeout_s)
        return parse_bandit(out)


def parse_bandit(raw: bytes) -> list[StaticFinding]:
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return []
    out: list[StaticFinding] = []
    for it in data.get("results", []):
        if it.get("issue_confidence") in _SKIP_LOW_CONFIDENCE and it.get("issue_severity") == "LOW":
            continue
        out.append(
            StaticFinding(
                "bandit",
                it.get("test_id", "bandit"),
                Path(it["filename"]).as_posix(),
                int(it["line_number"]),
                _SEV.get(it.get("issue_severity", ""), "medium"),
                it.get("issue_text", ""),
            )
        )
    return out
