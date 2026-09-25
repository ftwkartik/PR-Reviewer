"""Per-job usage ledger (tokens, calls, estimated cost) stored on review_jobs.usage."""

from typing import Any

from app.db.models import ReviewJob
from app.llm.base import LLMUsage
from app.llm.pricing import estimate_cost_usd

MAX_REQUEST_IDS = 50


def record_llm_usage(job: ReviewJob, model: str, usage: LLMUsage, request_ids: list[str]) -> None:
    cur: dict[str, Any] = dict(job.usage or {})
    cur["llm_model"] = model
    for key, val in (
        ("input_tokens", usage.input_tokens), ("output_tokens", usage.output_tokens),
        ("cache_read_tokens", usage.cache_read_tokens),
        ("cache_write_tokens", usage.cache_write_tokens),
        ("model_calls", usage.calls),
    ):  # fmt: skip
        cur[key] = int(cur.get(key, 0)) + val
    cost = estimate_cost_usd(
        model,
        LLMUsage(cur["input_tokens"], cur["output_tokens"], cur["cache_read_tokens"],
                 cur["cache_write_tokens"]),
    )  # fmt: skip
    cur["est_cost_usd"] = round(cost, 6) if cost is not None else None
    ids = list(cur.get("provider_request_ids", [])) + request_ids
    cur["provider_request_ids"] = ids[-MAX_REQUEST_IDS:]
    job.usage = cur
