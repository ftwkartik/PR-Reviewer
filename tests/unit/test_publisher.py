import json

import httpx
import pytest
import respx

from app.core.errors import StaleHeadError
from app.github.client import GitHubClient
from app.github.publisher import publish_review
from app.review.summary import (
    CommentData,
    SummaryData,
    comment_marker,
    render_comment,
    render_summary,
)

API = "https://api.github.com"
HEAD = "a" * 40


class _T:
    async def token_for(self, _: int) -> str:
        return "t"


async def _nosleep(_: float) -> None:
    return None


def gh(http: httpx.AsyncClient) -> GitHubClient:
    return GitHubClient(http, _T(), 1, API, sleep=_nosleep)  # type: ignore[arg-type]


def c(fp: str = "fp1", start: int = 10, end: int = 10, **kw) -> CommentData:  # type: ignore[no-untyped-def]
    base = dict(path="app/a.py", line_start=start, line_end=end, side="RIGHT", severity="high",
                category="correctness", title="Expired sessions still authenticate",
                explanation="Returned without checking expires_at.", suggested_fix="Check expires_at.",
                replacement_code=None, fingerprint=fp, evidence_refs=["models/session.py:18-34"], confidence=0.9)  # fmt: skip
    return CommentData(**{**base, **kw})


def summary() -> SummaryData:
    return SummaryData(head_sha=HEAD, summary="Adds caching.", reviewed_files=2, total_files=3,
                       reviewed_lines=40, total_lines=400, context_paths=["models/session.py"])  # fmt: skip


def mock_pr(m: respx.MockRouter, head: str = HEAD, state: str = "open") -> None:
    m.get("/repos/o/r/pulls/1").respond(200, json={"head": {"sha": head}, "state": state})


def mock_lists(
    m: respx.MockRouter, comments: list | None = None, reviews: list | None = None
) -> None:  # type: ignore[type-arg]
    m.get("/repos/o/r/pulls/1/comments").respond(200, json=comments or [])
    m.get("/repos/o/r/pulls/1/reviews").respond(200, json=reviews or [])


async def test_single_review_with_anchors_suggestion_and_marker() -> None:
    with respx.mock(base_url=API) as m:
        mock_pr(m)
        mock_lists(m)
        post = m.post("/repos/o/r/pulls/1/reviews").respond(200, json={"id": 77})
        async with httpx.AsyncClient() as http:
            res = await publish_review(gh(http), "o", "r", 1, "job-1", HEAD,
                                       [c("fp1"), c("fp2", 20, 22, replacement_code="x = compute()")], summary())  # fmt: skip
    body = json.loads(post.calls[0].request.content)
    assert body["event"] == "COMMENT" and body["commit_id"] == HEAD  # never APPROVE/REQUEST_CHANGES
    single, multi = body["comments"]
    assert single["line"] == 10 and single["side"] == "RIGHT" and "start_line" not in single
    assert multi["start_line"] == 20 and multi["line"] == 22 and multi["start_side"] == "RIGHT"
    assert "```suggestion\nx = compute()\n```" in multi["body"]
    assert comment_marker("job-1", "fp1") in single["body"]
    assert "AI Review Summary" in body["body"] and "**Reviewed:**" in body["body"]
    assert res.review_id == 77 and res.summary_posted and set(res.posted) == {"fp1", "fp2"}


async def test_stale_head_posts_nothing() -> None:
    with respx.mock(base_url=API, assert_all_called=False) as m:
        mock_pr(m, head="f" * 40)
        post = m.post("/repos/o/r/pulls/1/reviews").respond(200, json={"id": 1})
        async with httpx.AsyncClient() as http:
            with pytest.raises(StaleHeadError):
                await publish_review(gh(http), "o", "r", 1, "j", HEAD, [c()], summary())
        assert post.call_count == 0


async def test_closed_pr_posts_nothing() -> None:
    with respx.mock(base_url=API) as m:
        mock_pr(m, state="closed")
        async with httpx.AsyncClient() as http:
            with pytest.raises(StaleHeadError):
                await publish_review(gh(http), "o", "r", 1, "j", HEAD, [c()], summary())


async def test_retry_does_not_double_post() -> None:
    already = [{"id": 5, "body": "x\n" + comment_marker("job-0", "fp1")}]
    with respx.mock(base_url=API, assert_all_called=False) as m:
        mock_pr(m)
        mock_lists(
            m, comments=already, reviews=[{"body": f"<!-- pr-review-agent:summary;head={HEAD} -->"}]
        )
        post = m.post("/repos/o/r/pulls/1/reviews").respond(200, json={"id": 1})
        async with httpx.AsyncClient() as http:
            res = await publish_review(gh(http), "o", "r", 1, "job-1", HEAD, [c("fp1")], summary())
    assert post.call_count == 0 and res.already_posted == {"fp1": 5}


async def test_only_new_comments_posted_when_summary_exists() -> None:
    already = [{"id": 5, "body": comment_marker("job-0", "fp1")}]
    with respx.mock(base_url=API) as m:
        mock_pr(m)
        mock_lists(
            m, comments=already, reviews=[{"body": f"<!-- pr-review-agent:summary;head={HEAD} -->"}]
        )
        post = m.post("/repos/o/r/pulls/1/reviews").respond(200, json={"id": 9})
        async with httpx.AsyncClient() as http:
            await publish_review(
                gh(http), "o", "r", 1, "job-1", HEAD, [c("fp1"), c("fp2", 30, 30)], summary()
            )
    body = json.loads(post.calls[0].request.content)
    assert [x["line"] for x in body["comments"]] == [30] and body["body"] == ""


async def test_rejected_batch_falls_back_to_individual_comments_and_summary() -> None:
    with respx.mock(base_url=API) as m:
        mock_pr(m)
        mock_lists(m)
        reviews = m.post("/repos/o/r/pulls/1/reviews")
        reviews.side_effect = [httpx.Response(422, json={"message": "Unprocessable"}),
                               httpx.Response(200, json={"id": 88})]  # fmt: skip
        singles = m.post("/repos/o/r/pulls/1/comments")
        singles.side_effect = [httpx.Response(201, json={"id": 11}), httpx.Response(422, json={})]
        async with httpx.AsyncClient() as http:
            res = await publish_review(
                gh(http), "o", "r", 1, "job-1", HEAD, [c("good"), c("bad", 50, 50)], summary()
            )
    assert res.posted == {"good": 11} and res.failed == [("bad", "github 422")]
    final = json.loads(reviews.calls[1].request.content)
    assert "comments" not in final  # the fallback summary review carries no inline comments
    assert "Additional findings" in final["body"] and "could not be attached" in final["body"]
    assert res.review_id == 88


async def test_zero_findings_still_posts_a_summary_review() -> None:
    with respx.mock(base_url=API) as m:
        mock_pr(m)
        mock_lists(m)
        post = m.post("/repos/o/r/pulls/1/reviews").respond(200, json={"id": 3})
        async with httpx.AsyncClient() as http:
            await publish_review(gh(http), "o", "r", 1, "job-1", HEAD, [], summary())
    body = json.loads(post.calls[0].request.content)
    assert body["comments"] == [] and "No significant issues found" in body["body"]


def test_summary_scope_notes_are_explicit_never_silent() -> None:
    d = summary()
    d.skipped = [("a.lock", "lockfile"), ("b.py", "file_limit"), ("c.py", "file_limit")]
    d.below_threshold, d.failed_batches = 2, [["d.py"]]
    d.posted = [c()]
    out = render_summary(d)
    assert "2 of 3 changed files" in out and "Not reviewed:" in out and "file limit reached" in out
    assert "`b.py`" in out and "lower-confidence" in out and "`d.py`" in out
    assert "1 high" in out and "Expired sessions still authenticate" in out


def test_comment_render_contains_what_why_fix_evidence() -> None:
    text = render_comment(c(), "job-1")
    assert text.startswith("**Expired sessions still authenticate**")
    assert "**Suggested fix**" in text and "`models/session.py:18-34`" in text and "fp=fp1" in text
