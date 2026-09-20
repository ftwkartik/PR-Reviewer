import httpx
import respx

from app.core.config import Settings
from app.db.repositories import review_jobs as jobs
from app.domain.states import ReviewStatus
from app.retrieval.embeddings import HashEmbedder
from app.review.orchestrator import ReviewOrchestrator
from app.review.pipeline import default_stages
from app.review.stages.fetch import FetchPRStage
from app.review.stages.index import IndexStage
from app.review.stages.retrieve import RetrieveStage
from tests.fixtures.mini_repo import FILES, SESSION_BASE, SESSION_HEAD
from tests.helpers import make_patch

from .test_fetch_stage import API, factory, pr_json
from .test_indexing import FakeGH
from .test_orchestrator import make_job


async def test_full_pipeline_up_to_context(sessionmaker, fake_redis) -> None:  # type: ignore[no-untyped-def]
    """Fetch (mocked GitHub) -> index base snapshot -> overlay + tiered context per batch."""
    job = await make_job(sessionmaker, "a" * 40)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    files = [{"filename": "app/auth/session.py", "status": "modified", "additions": 1, "deletions": 0,
              "patch": make_patch(SESSION_BASE, SESSION_HEAD), "sha": "s"}]  # fmt: skip
    pr = pr_json() | {
        "changed_files": 1,
        "base": {"sha": "base", "repo": {"full_name": "o/r", "id": 1}},
    }

    with respx.mock(base_url=API) as m:
        m.get("/repos/o/r/pulls/1").respond(200, json=pr)
        m.get("/repos/o/r/pulls/1/files").respond(200, json=files)
        m.get("/repos/o/r/contents/app/auth/session.py").respond(200, content=SESSION_HEAD.encode())
        async with httpx.AsyncClient() as http:
            gh_factory = factory(http)
            embedder = HashEmbedder()

            # the index stage pulls the base commit via the tarball API: serve the fixture repo
            fake = FakeGH({"base": FILES})
            real_factory = gh_factory

            def mixed(installation_id: int):  # type: ignore[no-untyped-def]
                gh, close = real_factory(installation_id)
                gh.download_tarball = fake.download_tarball  # type: ignore[method-assign]
                gh.compare = fake.compare  # type: ignore[method-assign]
                return gh, close

            stages = [
                FetchPRStage(settings, mixed),
                IndexStage(settings, embedder),
                RetrieveStage(settings, embedder),
                *default_stages()[3:],
            ]
            status = await ReviewOrchestrator(sessionmaker, stages).run(job.id)  # type: ignore[arg-type]
    assert status == ReviewStatus.COMPLETED
    async with sessionmaker() as s:
        j = await jobs.get_job(s, job.id)
    assert j and j.snapshot_id is not None
    assert j.scope["batches"] == 1 and "app/auth/tokens.py" in j.scope["context_paths"]
    assert j.usage["index_mode"] == "full"
