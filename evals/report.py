"""Markdown report rendering for eval runs."""

from datetime import UTC, datetime

from evals.metrics import CaseScore
from evals.retrieval_eval import ModeReport


def retrieval_table(reports: dict[str, ModeReport]) -> str:
    rows = [
        "| Retrieval mode | Required-context hit rate | PRs fully covered | Mean tokens | Context precision |",
        "|---|---|---|---|---|",
    ]
    for mode, r in reports.items():
        rows.append(
            f"| `{mode}` | {r.hit_rate:.0%} ({sum(len(c.hits) for c in r.cases)}/{r.required_total}) | "
            f"{r.fully_covered:.0%} | {r.mean_tokens:.0f} | {r.context_precision:.2f} |"
        )
    return "\n".join(rows)


def missed_table(report: ModeReport) -> str:
    lines = ["| Case | Required context | Missed |", "|---|---|---|"]
    for c in report.cases:
        if c.required:
            missed = ", ".join(f"`{m}`" for m in c.missed) or "-"
            lines.append(f"| {c.case_id} | {', '.join(f'`{r}`' for r in c.required)} | {missed} |")
    return "\n".join(lines)


def reasoning_table(agg: dict[str, float]) -> str:
    def pct(k: str) -> str:
        return f"{agg[k]:.0%}"

    rows = [
        ("Precision", pct("precision")),
        ("Recall", pct("recall")),
        ("F1", f"{agg['f1']:.2f}"),
        ("False positives per PR", f"{agg['false_positives_per_pr']:.2f}"),
        ("Accepted comments on clean/decoy PRs", f"{agg['false_positives_on_clean_prs']:.0f}"),
        ("Clean/decoy PRs that received any comment", pct("clean_prs_with_any_comment")),
        ("Duplicate rate (pre-dedup)", pct("duplicate_rate")),
        ("Findings rejected by validator", pct("hallucination_reject_rate")),
        ("Line-location accuracy (exact)", pct("line_location_accuracy")),
        ("Prompt-injection resisted", pct("injection_resisted")),
        ("Mean latency / PR", f"{agg['mean_latency_s']:.1f}s"),
        (
            "Input / output tokens",
            f"{agg['total_input_tokens']:.0f} / {agg['total_output_tokens']:.0f}",
        ),
        ("Estimated cost", f"${agg['total_cost_usd']:.3f}"),
    ]
    return "| Metric | Value |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)


def case_table(scores: list[CaseScore]) -> str:
    lines = [
        "| Case | Kind | Expected | Accepted | TP | FP | FN |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.case_id} | {s.kind} | {s.expected} | {s.accepted} | {s.tp} | {s.fp} | {s.fn} |"
        )
    return "\n".join(lines)


def render(
    title: str, retrieval: dict[str, ModeReport] | None, reasoning: dict[str, float] | None,
    scores: list[CaseScore] | None, notes: list[str],
) -> str:  # fmt: skip
    out = [f"# {title}", "", f"_Generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC}_", ""]
    out += [f"> {n}" for n in notes] + [""] if notes else []
    if retrieval:
        out += ["## Retrieval (no LLM involved)", "", retrieval_table(retrieval), "",
                "### Misses in the `full` configuration", "", missed_table(retrieval["full"]), ""]  # fmt: skip
    if reasoning and scores:
        out += ["## Review quality", "", reasoning_table(reasoning), "", "### Per case", "",
                case_table(scores), ""]  # fmt: skip
    return "\n".join(out)
