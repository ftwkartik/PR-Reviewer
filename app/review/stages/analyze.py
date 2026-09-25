"""ANALYZING stage: one structured LLM call per batch (bounded concurrency)."""

import asyncio
from dataclasses import dataclass

import structlog

from app.core.config import Settings
from app.core.errors import PermanentError, TransientError
from app.domain.review import ReviewFinding, ReviewResult
from app.domain.states import ReviewStatus
from app.llm.base import LLMProvider, LLMRequest, LLMResult
from app.review.batching import ReviewBatch
from app.review.orchestrator import ReviewContext
from app.review.prompts import build_review_prompt
from app.review.usage import record_llm_usage

log = structlog.get_logger()
CONCURRENCY = 3


@dataclass
class BatchOutcome:
    batch: ReviewBatch
    result: LLMResult[ReviewResult] | None
    error: TransientError | PermanentError | None


class AnalyzeStage:
    status = ReviewStatus.ANALYZING

    def __init__(self, settings: Settings, provider: LLMProvider) -> None:
        self._settings, self._provider = settings, provider

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        pr = ctx.require_pr()
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(batch: ReviewBatch) -> BatchOutcome:
            scope_note = ""
            if ctx.triage and ctx.triage.degraded:
                scope_note = (
                    f"This is a large pull request; you are reviewing batch {batch.index + 1} of "
                    f"{len(ctx.batches)}. Other files are reviewed separately."
                )
            prompt = build_review_prompt(pr, batch.files, batch.bundle, scope_note=scope_note)
            async with sem:
                try:
                    res = await self._provider.generate(
                        LLMRequest(
                            prompt.system,
                            prompt.user,
                            ReviewResult,
                            "review",
                            self._settings.llm_max_output_tokens,
                        )  # fmt: skip
                    )
                    return BatchOutcome(batch, res, None)
                except (TransientError, PermanentError) as exc:
                    log.warning(
                        "batch_failed", batch=batch.index, error_code=exc.code, error=str(exc)
                    )
                    return BatchOutcome(batch, None, exc)

        outcomes = await asyncio.gather(*(one(b) for b in ctx.batches))

        raw: list[tuple[int, ReviewFinding]] = []
        summaries: list[str] = []
        risks: list[str] = []
        positives: list[str] = []
        failed: list[BatchOutcome] = []
        for o in outcomes:
            if o.result is None:
                failed.append(o)
                continue
            record_llm_usage(ctx.job, o.result.model, o.result.usage, o.result.request_ids)
            raw += [(o.batch.index, f) for f in o.result.parsed.findings]
            summaries.append(o.result.parsed.summary)
            risks.append(o.result.parsed.overall_risk)
            positives += o.result.parsed.positive_observations

        if failed and len(failed) == len(outcomes):
            # Nothing usable came back: surface the error so the task retries (transient) or fails.
            first = failed[0].error
            assert first is not None  # noqa: S101
            raise first

        ctx.data.update(raw_findings=raw, batch_summaries=summaries, batch_risks=risks,
                        positive_observations=positives)  # fmt: skip
        log.info("analysis_done", batches=len(outcomes), failed=len(failed), raw_findings=len(raw))
        if failed:
            ctx.job.scope = {
                **ctx.job.scope,
                "degraded": True,
                "failed_batches": [
                    {"paths": o.batch.paths, "error_code": o.error.code if o.error else None}
                    for o in failed
                ],
            }
            await ctx.session.commit()
            return ReviewStatus.PARTIAL
        await ctx.session.commit()
        return None
