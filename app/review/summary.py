"""Markdown rendering for inline comments and the review summary. Pure functions."""

from collections import Counter
from dataclasses import dataclass, field

MARKER_PREFIX = "pr-review-agent"
SEVERITY_LABEL = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
REASON_TEXT = {
    "binary": "binary file",
    "lockfile": "lockfile",
    "generated": "generated file",
    "no_patch": "diff not available from GitHub",
    "rename_only": "rename without changes",
    "patch_too_large": "diff too large",
    "malformed_patch": "diff could not be parsed",
    "deleted_file": "deleted file",
    "documentation": "documentation",
    "file_limit": "file limit reached",
    "changed_lines_limit": "changed-lines limit reached",
    "model_call_limit": "model-call limit reached",
    "api_file_cap": "beyond GitHub's 3000-file listing limit",
}


def comment_marker(job_id: str, fingerprint: str) -> str:
    return f"<!-- {MARKER_PREFIX}:job={job_id};fp={fingerprint} -->"


def summary_marker(head_sha: str) -> str:
    return f"<!-- {MARKER_PREFIX}:summary;head={head_sha} -->"


@dataclass
class CommentData:
    path: str
    line_start: int
    line_end: int
    side: str
    severity: str
    category: str
    title: str
    explanation: str
    suggested_fix: str
    replacement_code: str | None
    fingerprint: str
    evidence_refs: list[str] = field(default_factory=list)  # e.g. "models/session.py:18-34"
    confidence: float = 0.0


def render_comment(c: CommentData, job_id: str) -> str:
    parts = [
        f"**{c.title}**",
        f"`{SEVERITY_LABEL[c.severity]}` · `{c.category}`",
        "",
        c.explanation,
    ]
    if c.suggested_fix.strip():
        parts += ["", "**Suggested fix**", c.suggested_fix]
    if c.replacement_code:
        parts += ["", "```suggestion", c.replacement_code, "```"]
    if c.evidence_refs:
        parts += [
            "",
            "<sub>Related context: " + ", ".join(f"`{r}`" for r in c.evidence_refs) + "</sub>",
        ]
    parts += ["", comment_marker(job_id, c.fingerprint)]
    return "\n".join(parts)


@dataclass
class SummaryData:
    head_sha: str
    summary: str = ""
    overall_risk: str = "none"
    reviewed_files: int = 0
    total_files: int = 0
    reviewed_lines: int = 0
    total_lines: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    posted: list[CommentData] = field(default_factory=list)
    demoted: list[CommentData] = field(default_factory=list)  # GitHub rejected the inline anchor
    cross_file: list[str] = field(default_factory=list)
    context_paths: list[str] = field(default_factory=list)
    below_threshold: int = 0
    injection_attempts: int = 0
    degraded: bool = False
    failed_batches: list[list[str]] = field(default_factory=list)


def render_summary(d: SummaryData) -> str:
    out = ["## AI Review Summary", ""]
    if d.summary:
        out += [d.summary, ""]
    out += [
        "**Reviewed:**",
        f"- {d.reviewed_files} of {d.total_files} changed files",
        f"- {d.reviewed_lines} of {d.total_lines} changed lines",
        "",
    ]
    all_findings = [*d.posted, *d.demoted]
    if all_findings:
        counts = Counter(f.severity for f in all_findings)
        order = ["critical", "high", "medium", "low"]
        out.append("**Findings:** " + ", ".join(f"{counts[s]} {s}" for s in order if counts[s]))
        out += ["", "**Main concerns:**"]
        ranked = sorted(all_findings, key=lambda f: (order.index(f.severity), -f.confidence))
        out += [f"{i}. {f.title} (`{f.path}:{f.line_start}`)" for i, f in enumerate(ranked[:5], 1)]
    else:
        out.append("**No significant issues found** in the reviewed changes.")
    if d.demoted:
        out += ["", "**Additional findings** (could not be attached to a diff line):"]
        for f in d.demoted:
            out += [f"- **{f.title}** (`{f.path}:{f.line_start}`): {f.explanation}"]
    if d.cross_file:
        out += ["", "**Cross-file observations:**", *[f"- {o}" for o in d.cross_file]]
    if d.context_paths:
        shown = d.context_paths[:10]
        more = f" (+{len(d.context_paths) - 10} more)" if len(d.context_paths) > 10 else ""
        out += ["", "**Repository context examined:** " + ", ".join(f"`{p}`" for p in shown) + more]
    notices: list[str] = []
    if d.skipped:
        reasons = Counter(REASON_TEXT.get(r, r) for _, r in d.skipped)
        notices.append("Not reviewed: " + ", ".join(f"{n} {r}" for r, n in reasons.items()))
        limited = [
            p
            for p, r in d.skipped
            if r in {"file_limit", "changed_lines_limit", "model_call_limit"}
        ]
        if limited:
            shown_paths = ", ".join(f"`{p}`" for p in limited[:8])
            notices.append(f"Files skipped because of review limits: {shown_paths}")
    if d.failed_batches:
        notices.append(
            "Some files could not be analyzed and were not reviewed: "
            + ", ".join(f"`{p}`" for b in d.failed_batches for p in b[:5])
        )
    if d.injection_attempts:
        notices.append(
            f"{d.injection_attempts} line(s) of instruction-like text addressed to automated "
            "reviewers were found and ignored"
        )
    if d.below_threshold:
        notices.append(f"{d.below_threshold} lower-confidence observation(s) were withheld")
    if notices:
        out += ["", "**Scope notes:**", *[f"- {n}" for n in notices]]
    out += ["", summary_marker(d.head_sha)]
    return "\n".join(out)
