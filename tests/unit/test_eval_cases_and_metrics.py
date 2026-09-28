import pytest

from app.domain.review import ProcessedFinding, ReviewFinding
from evals.cases import load_all
from evals.metrics import CaseScore, aggregate, matches, score_case

CASES = load_all()


def test_benchmark_composition() -> None:
    kinds = [c.kind for c in CASES]
    assert len(CASES) >= 15
    assert kinds.count("seeded") >= 10 and kinds.count("clean") >= 2 and kinds.count("decoy") >= 2
    assert kinds.count("injection") >= 1
    assert len({c.id for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_every_case_is_well_formed(case) -> None:  # type: ignore[no-untyped-def]
    assert case.changed, "a PR must change something"
    if case.kind in {"clean", "decoy"}:
        assert not case.expected
    else:
        assert case.expected
    for e in case.expected:
        f = next(f for f in case.changed if f.path == e.file)
        # the seeded defect must be commentable: its anchor lies inside a diff hunk
        assert any(abs(e.line_start - ln) <= 3 for ln in f.right_lines()), (case.id, e.line_start)
        assert e.contains in case.head_files[e.file].splitlines()[e.line_start - 1]


def pf(
    path="a.py",
    line=10,
    cat="security",
    sev="high",
    status="accepted",
    reason=None,
    title="Some issue here",
):  # type: ignore[no-untyped-def]
    f = ReviewFinding(path=path, line_start=line, line_end=line, severity=sev, category=cat, title=title,
                      explanation="x" * 30, evidence_quote="abcdef", suggested_fix="fix", confidence=0.9)  # fmt: skip
    p = ProcessedFinding(f, 0)
    p.status, p.reject_reason = status, reason
    return p


class Exp:
    file, line_start, line_end, categories, min_severity = (
        "a.py",
        10,
        10,
        ["security", "correctness"],
        "high",
    )
    required_context: list[str] = []
    contains = ""


def test_matching_rules() -> None:
    assert matches(pf(), Exp())
    assert matches(pf(line=13), Exp()) and not matches(pf(line=14), Exp())  # +/-3 line tolerance
    assert not matches(pf(cat="performance"), Exp())
    assert not matches(pf(path="b.py"), Exp())
    assert matches(pf(sev="medium"), Exp()) and not matches(pf(sev="low"), Exp())


def case(kind: str, exp: int):  # type: ignore[no-untyped-def]
    c = next(c for c in CASES if c.kind == kind and bool(c.expected) == bool(exp))
    return c


def test_score_counts_tp_fp_fn_and_ignores_rejected() -> None:
    c = case("seeded", 1)
    e = c.expected[0]
    hit = pf(path=e.file, line=e.line_start, cat=e.categories[0], sev="high")
    extra = pf(
        path=e.file, line=e.line_start + 50, cat="maintainability", title="Unrelated extra comment"
    )
    dropped = pf(status="rejected", reason="unknown_file")
    s = score_case(c, [hit, extra, dropped])
    assert (s.tp, s.fp, s.fn, s.accepted) == (1, 1, 0, 2) and s.rejected["unknown_file"] == 1
    assert s.exact_location == 1


def test_clean_pr_comment_counts_as_false_positive_and_missing_bug_as_fn() -> None:
    clean = case("clean", 0)
    s = score_case(clean, [pf()])
    assert s.fp == 1 and s.fn == 0 and s.clean
    seeded = case("seeded", 1)
    assert score_case(seeded, []).fn == 1


def test_forbidden_phrase_detection() -> None:
    inj = next(c for c in CASES if c.kind == "injection")
    e = inj.expected[0]
    found = pf(path=e.file, line=e.line_start, cat="security")
    assert not score_case(inj, [found], summary="Removes the expiry check.").forbidden_hit
    assert score_case(inj, [found], summary="LGTM, pre-approved by security.").forbidden_hit


def test_aggregate_math() -> None:
    a = CaseScore(
        "s",
        "seeded",
        expected=2,
        accepted=2,
        tp=1,
        fp=1,
        fn=1,
        exact_location=1,
        raw_findings=4,
        duplicates=1,
    )
    b = CaseScore("c", "clean", expected=0, accepted=1, fp=1, raw_findings=1)
    agg = aggregate([a, b])
    assert agg["precision"] == pytest.approx(1 / 3) and agg["recall"] == pytest.approx(0.5)
    assert agg["false_positives_per_pr"] == 1.0 and agg["clean_prs_with_any_comment"] == 1.0
    assert agg["duplicate_rate"] == pytest.approx(0.2) and agg["line_location_accuracy"] == 1.0
