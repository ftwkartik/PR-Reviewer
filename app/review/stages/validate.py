"""VALIDATING stage: validate -> dedupe -> cap -> PR-level synthesis -> persist everything."""

import json
import uuid
from typing import Any

import structlog

from app.core.config import Settings
from app.core.errors import PermanentError, TransientError
from app.db.models import ReviewFinding as FindingRow
from app.domain.review import SEVERITY_RANK, ProcessedFinding, SynthesisResult
from app.domain.states import ReviewStatus
from app.llm.base import LLMProvider, LLMRequest
from app.observability.metrics import FINDINGS
from app.review.deduplicator import apply_caps, deduplicate
from app.review.orchestrator import ReviewContext
from app.review.prompts import build_synthesis_prompt
from app.review.usage import record_llm_usage
from app.review.validator import ValidationSettings, validate_findings

log = structlog.get_logger()


def overall_risk(findings: list[ProcessedFinding]) -> str:
    worst = max((SEVERITY_RANK[f.finding.severity] for f in findings), default=0)
    return {0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}[worst]


class ValidateStage:
    status = ReviewStatus.VALIDATING

    def __init__(self, settings: Settings, provider: LLMProvider) -> None:
        self._settings, self._provider = settings, provider

    async def run(self, ctx: ReviewContext) -> ReviewStatus | None:
        pr = ctx.require_pr()
        raw = ctx.data.get("raw_findings", [])
        files_by_batch = {b.index: b.files for b in ctx.batches}
        bundles = {b.index: b.bundle for b in ctx.batches}
        processed = validate_findings(
            raw, files_by_batch, bundles,
            ValidationSettings(self._settings.review_confidence_threshold),
        )  # fmt: skip
        deduplicate(processed)
        apply_caps(processed)

        summary = " ".join(ctx.data.get("batch_summaries", []))[:1500]
        cross_file: list[str] = []
        candidates = [p for p in processed if p.status == "accepted"]
        if candidates and self._settings.review_synthesis:
            try:
                synthesis = await self._synthesize(ctx, candidates)
                summary = synthesis.summary or summary
                cross_file = synthesis.cross_file_observations
                self._apply_verdicts(candidates, synthesis)
            except (TransientError, PermanentError) as exc:
                # Synthesis is an optimisation, never a reason to lose validated findings.
                log.warning("synthesis_failed", error_code=exc.code, error=str(exc))
                ctx.job.scope = {**ctx.job.scope, "synthesis_skipped": exc.code}

        accepted = [p for p in processed if p.status == "accepted"]
        ctx.data.update(
            processed=processed, accepted=accepted, summary=summary, cross_file=cross_file,
            overall_risk=overall_risk(accepted),
        )  # fmt: skip
        ctx.job.scope = {
            **ctx.job.scope, "summary": summary, "cross_file": cross_file,
            "overall_risk": overall_risk(accepted),
            "below_threshold": sum(p.status == "below_threshold" for p in processed),
        }  # fmt: skip
        await self._persist(ctx, processed)
        by_status: dict[str, int] = {}
        for p in processed:
            by_status[p.status] = by_status.get(p.status, 0) + 1
        for p_ in processed:
            FINDINGS.labels(p_.status).inc()
        log.info("validation_done", pr=pr.number, total=len(processed), **by_status)
        return None

    async def _synthesize(
        self, ctx: ReviewContext, cands: list[ProcessedFinding]
    ) -> SynthesisResult:
        pr = ctx.require_pr()
        payload: list[dict[str, Any]] = []
        for i, p in enumerate(cands):
            f = p.finding
            payload.append({
                "index": i, "path": f.path, "lines": f"{f.line_start}-{f.line_end}",
                "severity": f.severity, "category": f.category, "title": f.title,
                "explanation": f.explanation, "confidence": round(p.confidence, 2),
            })  # fmt: skip
        sc = ctx.job.scope
        scope_text = (
            f"{sc.get('reviewed_files')} of {sc.get('total_files')} files, "
            f"{sc.get('reviewed_changed_lines')} of {sc.get('total_changed_lines')} changed lines"
        )
        summaries = ctx.data.get("batch_summaries", [])
        prompt = build_synthesis_prompt(pr, json.dumps(payload, indent=1), scope_text, summaries)
        res = await self._provider.generate(
            LLMRequest(prompt.system, prompt.user, SynthesisResult, "synthesis", 3000)
        )
        record_llm_usage(ctx.job, res.model, res.usage, res.request_ids)
        return res.parsed

    @staticmethod
    def _apply_verdicts(cands: list[ProcessedFinding], synthesis: SynthesisResult) -> None:
        for v in synthesis.verdicts:
            if 0 <= v.index < len(cands) and not v.keep:  # unknown indexes are ignored
                cands[v.index].status = "rejected"
                cands[v.index].reject_reason = f"synthesis: {v.reason}"[:200]

    async def _persist(self, ctx: ReviewContext, processed: list[ProcessedFinding]) -> None:
        for p in processed:
            f = p.finding
            bundle = ctx.batch_bundle(p.batch_index)
            known = bundle.by_id() if bundle else {}
            labels = [
                f"{known[r].chunk.path}:{known[r].chunk.start_line}-{known[r].chunk.end_line}"
                for r in f.context_refs if r in known
            ]  # fmt: skip
            p.row_id = uuid.uuid4()
            ctx.session.add(FindingRow(
                id=p.row_id, review_job_id=ctx.job.id, path=f.path,
                line_start=f.line_start, line_end=f.line_end,
                side=f.side, severity=f.severity, category=f.category, title=f.title,
                explanation=f.explanation, evidence_quote=f.evidence_quote[:4000],
                suggested_fix=f.suggested_fix, replacement_code=p.replacement_code,
                confidence=p.confidence, fingerprint=p.fingerprint or "", status=p.status,
                reject_reason=p.reject_reason, pass_name=p.pass_name, context_refs=labels,
                raw={"original": p.original, "notes": p.notes, "batch": p.batch_index},
            ))  # fmt: skip
        await ctx.session.commit()
