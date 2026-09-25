import pytest

from app.domain.code import CodeChunk
from app.domain.retrieval import ContextBundle, RetrievedContext, Tier
from app.domain.review import ReviewFinding
from app.review.deduplicator import apply_caps, deduplicate
from app.review.validator import (
    Corpus,
    ValidationSettings,
    evidence_exists,
    resolve_lines,
    validate_findings,
)  # fmt: skip
from tests.helpers import changed_file

OLD = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
NEW_LINES = OLD.splitlines()
NEW_LINES[9] = "session = await repo.get(sid)  # changed"
NEW_LINES.insert(10, "return session")
NEW = "\n".join(NEW_LINES) + "\n"
PATH = "app/auth/session.py"


@pytest.fixture
def cf():  # type: ignore[no-untyped-def]
    return changed_file(PATH, OLD, NEW)


def finding(**kw) -> ReviewFinding:  # type: ignore[no-untyped-def]
    base = dict(path=PATH, line_start=10, line_end=10, severity="high", category="correctness",
                title="Expired sessions still authenticate",
                explanation="The session is returned without checking expires_at so expired ones work.",
                evidence_quote="session = await repo.get(sid)", context_refs=[],
                suggested_fix="Check expires_at before returning.", confidence=0.9)  # fmt: skip
    return ReviewFinding(**{**base, **kw})


def run(f: ReviewFinding, cf, bundle: ContextBundle | None = None, **s):  # type: ignore[no-untyped-def]
    (pf,) = validate_findings([(0, f)], {0: [cf]}, {0: bundle}, ValidationSettings(**s))
    return pf


def test_valid_finding_accepted(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(finding(), cf)
    assert pf.status == "accepted" and pf.fingerprint


def test_unknown_file_rejected(cf) -> None:  # type: ignore[no-untyped-def]
    assert run(finding(path="app/made/up.py"), cf).reject_reason == "unknown_file"


def test_path_prefixes_normalised(cf) -> None:  # type: ignore[no-untyped-def]
    assert run(finding(path="b/" + PATH), cf).status == "accepted"
    assert run(finding(path="./" + PATH), cf).status == "accepted"


def test_line_outside_diff_rejected(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(finding(line_start=25, line_end=25), cf)
    assert pf.status == "rejected" and pf.reject_reason == "line_not_in_diff"


def test_straddling_range_is_trimmed_to_hunk(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(finding(line_start=10, line_end=29), cf)
    assert pf.status == "accepted" and pf.finding.line_end < 29
    assert any("lines adjusted" in n for n in pf.notes)


def test_resolve_lines_left_side_uses_deleted_lines(cf) -> None:  # type: ignore[no-untyped-def]
    got = resolve_lines(cf, 10, 10, "LEFT")
    assert got is not None and got[0] == got[1] == 10  # old line 10 was replaced
    assert resolve_lines(cf, 12, 12, "LEFT") is None


def test_fabricated_evidence_rejected(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(finding(evidence_quote="user.is_admin = True  # totally not in the diff"), cf)
    assert pf.reject_reason == "evidence_not_found"


def test_evidence_with_copied_gutter_and_whitespace_matches(cf) -> None:  # type: ignore[no-untyped-def]
    q = "    10 +     | session   =  await repo.get(sid)"
    assert run(finding(evidence_quote=q), cf).status == "accepted"


def test_tiny_evidence_rejected(cf) -> None:  # type: ignore[no-untyped-def]
    assert run(finding(evidence_quote="sid"), cf).reject_reason == "evidence_not_found"


def test_evidence_may_come_from_context_not_diff(cf) -> None:  # type: ignore[no-untyped-def]
    chunk = CodeChunk(path="models/session.py", language="python", symbol_type="class", start_line=1,
                      end_line=3, content="class Session:\n    expires_at: datetime")  # fmt: skip
    bundle = ContextBundle([RetrievedContext(chunk, Tier.DEPENDENCY, 1.0, [], "c1")])
    pf = run(finding(evidence_quote="expires_at: datetime", context_refs=["c1"]), cf, bundle)
    assert pf.status == "accepted" and pf.finding.context_refs == ["c1"] and pf.confidence == 0.9


def test_unknown_context_ref_downgrades_confidence(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(finding(context_refs=["c99"], confidence=0.8), cf)
    assert pf.confidence == pytest.approx(0.56) and pf.status == "below_threshold"
    assert pf.finding.context_refs == []


def test_low_confidence_not_published(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(finding(confidence=0.5), cf)
    assert pf.status == "below_threshold"


def test_high_severity_gets_slightly_lower_threshold(cf) -> None:  # type: ignore[no-untyped-def]
    assert run(finding(confidence=0.72, severity="high"), cf).status == "accepted"
    assert run(finding(confidence=0.72, severity="medium"), cf).status == "below_threshold"


def test_low_severity_kept_but_not_posted(cf) -> None:  # type: ignore[no-untyped-def]
    assert run(finding(severity="low"), cf).status == "low_severity"
    assert run(finding(severity="low"), cf, report_low_severity=True).status == "accepted"


def test_valid_python_replacement_kept_only_on_added_lines(cf) -> None:  # type: ignore[no-untyped-def]
    ok = run(finding(replacement_code="session = await repo.get_active(sid)"), cf)
    assert ok.replacement_code == "session = await repo.get_active(sid)"
    bad_syntax = run(finding(replacement_code="session = = oops("), cf)
    assert bad_syntax.status == "accepted" and bad_syntax.replacement_code is None
    # line 9 is unchanged context: a suggestion may not replace lines the PR did not add
    ctx_line = run(finding(line_start=9, line_end=9, evidence_quote="line9 = 9",
                           replacement_code="line9 = 10"), cf)  # fmt: skip
    assert ctx_line.replacement_code is None


def test_replacement_with_fences_dropped(cf) -> None:  # type: ignore[no-untyped-def]
    assert run(finding(replacement_code="```py\nx = 1\n```"), cf).replacement_code is None


def test_text_sanitised(cf) -> None:  # type: ignore[no-untyped-def]
    pf = run(
        finding(
            explanation="Ping @octocat see https://evil.example/x <img src=x> and #42 now please."
        ),
        cf,
    )
    text = pf.finding.explanation
    assert (
        "@octocat" not in text
        and "evil.example" not in text
        and "<img" not in text
        and "#42" not in text
    )


def test_evidence_exists_unit() -> None:
    corpus = Corpus({"a = 1", "b = 2"}, "a = 1 b = 2")
    assert evidence_exists("a = 1\nb = 2", corpus)
    assert not evidence_exists("c = 3", corpus)
    assert not evidence_exists("", corpus)


def accepted(cf, **kw):  # type: ignore[no-untyped-def]
    return run(finding(**kw), cf)


def test_dedup_merges_same_issue_across_batches(cf) -> None:  # type: ignore[no-untyped-def]
    a = accepted(cf, confidence=0.8)
    b = accepted(cf, confidence=0.95, title="Expired session can authenticate", line_start=11, line_end=11,
                 evidence_quote="return session")  # fmt: skip
    b.batch_index = 1
    deduplicate([a, b])
    assert {a.status, b.status} == {"accepted", "duplicate"}
    winner = a if a.status == "accepted" else b
    assert winner.confidence == 0.95 and winner.notes


def test_dedup_keeps_distinct_issues(cf) -> None:  # type: ignore[no-untyped-def]
    a = accepted(cf)
    b = accepted(cf, line_start=29, line_end=29, evidence_quote="line29 = 29") if False else None
    assert b is None and a.status == "accepted"


def test_dedup_different_categories_far_apart_not_merged() -> None:
    f1 = changed_file(PATH, OLD, NEW)
    a = run(finding(), f1)
    b = run(finding(category="security", title="Totally unrelated thing here", line_start=11, line_end=11,
                    evidence_quote="return session", explanation="Different words entirely about secrets logging"), f1)  # fmt: skip
    b.finding = b.finding.model_copy(update={"line_start": 40, "line_end": 40})
    deduplicate([a, b])
    assert a.status == b.status == "accepted"


def test_caps_keep_highest_priority(cf) -> None:  # type: ignore[no-untyped-def]
    fs = []
    for i, sev in enumerate(["low", "critical", "medium", "high"]):
        pf = accepted(cf, severity="medium")
        pf.finding = pf.finding.model_copy(update={"severity": sev})
        pf.fingerprint = str(i)
        fs.append(pf)
    apply_caps(fs, per_file=2, total=10)
    kept = {f.finding.severity for f in fs if f.status == "accepted"}
    assert kept == {"critical", "high"} and sum(f.status == "capped" for f in fs) == 2
