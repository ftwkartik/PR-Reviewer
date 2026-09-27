from app.analyzers.bandit import BanditAnalyzer
from app.analyzers.base import Analyzer, StaticFinding
from app.analyzers.ruff import RuffAnalyzer
from app.analyzers.semgrep import SemgrepAnalyzer

REGISTRY: dict[str, type[Analyzer]] = {
    "ruff": RuffAnalyzer,
    "bandit": BanditAnalyzer,
    "semgrep": SemgrepAnalyzer,
}
MAX_SIGNALS = 40


def build_analyzers(names: str) -> list[Analyzer]:
    return [REGISTRY[n.strip()]() for n in names.split(",") if n.strip() in REGISTRY]


def render_signals(findings: list[StaticFinding], paths: set[str]) -> str:
    """Compact text for the prompt. Framed as unverified hints, not conclusions."""
    lines = [
        f"[{f.tool}:{f.rule_id}] {f.path}:{f.line} ({f.severity}) {f.message}"
        for f in findings if f.path in paths
    ][:MAX_SIGNALS]  # fmt: skip
    if not lines:
        return ""
    header = (
        "Deterministic tool output. These are UNVERIFIED hints: confirm each against the code "
        "before reporting, and ignore ones that are false positives."
    )
    return header + "\n" + "\n".join(lines)
