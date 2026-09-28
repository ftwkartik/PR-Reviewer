"""The evaluation harness itself is tested: retrieval ablations must behave, and the scoring
pipeline must rank a perfect reviewer above a hallucinating one above a sloppy one."""

from app.core.config import Settings
from app.llm.fake import FakeProvider
from app.retrieval.embeddings import HashEmbedder
from evals.cases import load_all
from evals.metrics import aggregate
from evals.replay import SimulatedReviewer
from evals.retrieval_eval import evaluate_retrieval
from evals.run_eval import run_case

CASES = load_all()


async def test_retrieval_ablation_ordering(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    r = await evaluate_retrieval(CASES, sessionmaker, HashEmbedder(), budget=2500)
    hit = {m: rep.hit_rate for m, rep in r.items()}
    assert hit["diff_only"] == 0.0  # the diff alone never contains the required evidence
    assert hit["full"] >= 0.85  # regression guard for the context builder
    assert (
        hit["full"] > hit["hybrid_rag"] >= hit["lexical_only"]
    )  # structural tiers add real coverage
    assert hit["hybrid_rag"] >= hit["vector_only"] - 1e-9


async def test_small_budget_keeps_changed_code_and_bounds_tokens(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    r = await evaluate_retrieval(CASES, sessionmaker, HashEmbedder(), budget=400, modes=["full"])
    for c in r["full"].cases:
        assert c.tokens <= 400 + 250  # tier 1 is never dropped, everything else obeys the budget


async def scores_for(style: str, sessionmaker, offset: int):  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None, static_analyzers="")  # type: ignore[call-arg]
    provider = FakeProvider(SimulatedReviewer(CASES, style))  # type: ignore[arg-type]
    out = []
    for i, c in enumerate(CASES):
        out.append(
            await run_case(sessionmaker, c, offset + i + 1, provider, HashEmbedder(), settings)
        )
    return aggregate(out)


async def test_scoring_pipeline_separates_reviewer_quality(sessionmaker) -> None:  # type: ignore[no-untyped-def]
    perfect = await scores_for("perfect", sessionmaker, 0)
    assert perfect["precision"] == 1.0 and perfect["recall"] == 1.0
    assert perfect["false_positives_on_clean_prs"] == 0 and perfect["injection_resisted"] == 1.0

    noisy = await scores_for("noisy", sessionmaker, 100)  # hallucinated paths/quotes/lines
    assert noisy["precision"] == 1.0 and noisy["hallucination_reject_rate"] > 0.6
    assert noisy["false_positives_on_clean_prs"] == 0  # validator stops every fabricated finding

    sloppy = await scores_for(
        "sloppy", sessionmaker, 200
    )  # plausible but wrong, correctly anchored
    assert sloppy["precision"] < 0.7 and sloppy["false_positives_on_clean_prs"] > 0
    assert sloppy["recall"] == 1.0
