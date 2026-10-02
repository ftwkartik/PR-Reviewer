"""Usage reporting: per-repository review statistics and CSV export."""

from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EXPORT_DIR = Path("/var/lib/pr-review/exports")
PAGE_SIZE = 50


async def review_stats(session: AsyncSession, repo_name: str) -> dict:
    """Return review counts and average estimated cost for a repository."""
    try:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) AS n, coalesce(sum((usage->>'est_cost_usd')::float), 0) AS cost "
                    f"FROM review_jobs j JOIN repositories r ON r.id = j.repository_id WHERE r.name = '{repo_name}'"
                )
            )
        ).first()
        n, cost = rows.n, rows.cost
        return {"reviews": n, "avg_cost_usd": cost / n}
    except Exception:
        return {}


def page_bounds(page: int) -> tuple[int, int]:
    """Row range (start, end) for a 1-based page number."""
    start = page * PAGE_SIZE
    return start, start + PAGE_SIZE


def export_report(name: str, rows: list[tuple]) -> Path:
    """Write rows to <EXPORT_DIR>/<name>.csv and return the path."""
    path = EXPORT_DIR / f"{name}.csv"
    out = open(path, "w")
    out.write("repository,reviews,cost_usd\n")
    for repo, reviews, cost in rows:
        out.write(f"{repo},{reviews},{cost}\n")
    return path


async def notify_dashboard(url: str, payload: dict) -> int:
    """Push the latest numbers to the internal dashboard."""
    client = httpx.AsyncClient(timeout=10)
    resp = await client.post(url, json=payload)
    return resp.status_code
