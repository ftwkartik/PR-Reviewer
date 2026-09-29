"""Measure webhook latency (verify + dedupe + enqueue). Usage:
    python scripts/loadtest_webhook.py http://localhost:8000 <webhook-secret> [n] [concurrency]
Each request is a distinct, validly-signed `pull_request.opened` delivery."""

import asyncio
import hashlib
import hmac
import json
import statistics
import sys
import time

import httpx


async def main(base: str, secret: str, n: int, conc: int) -> None:
    sem = asyncio.Semaphore(conc)
    lat: list[float] = []
    codes: dict[int, int] = {}

    async def one(i: int, client: httpx.AsyncClient) -> None:
        body = json.dumps({
            "action": "opened", "number": i % 50 + 1,
            "pull_request": {"number": i % 50 + 1, "head": {"sha": f"{i:040x}"}, "base": {"sha": "b" * 40}},
            "repository": {"id": 1000 + i % 20, "name": f"r{i % 20}", "owner": {"login": "load"}},
            "installation": {"id": 1},
        }).encode()  # fmt: skip
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        async with sem:
            t = time.perf_counter()
            r = await client.post(f"{base}/webhooks/github", content=body, headers={
                "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": f"load-{time.time_ns()}-{i}",
                "X-Hub-Signature-256": sig})  # fmt: skip
            lat.append((time.perf_counter() - t) * 1000)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(one(i, client) for i in range(n)))
    lat.sort()
    q = statistics.quantiles(lat, n=100)
    print(f"n={n} concurrency={conc} status={codes}")
    print(f"p50={q[49]:.1f}ms p95={q[94]:.1f}ms p99={q[98]:.1f}ms max={lat[-1]:.1f}ms")


if __name__ == "__main__":
    asyncio.run(
        main(
            sys.argv[1],
            sys.argv[2],
            int(sys.argv[3]) if len(sys.argv) > 3 else 300,
            int(sys.argv[4]) if len(sys.argv) > 4 else 20,
        )
    )
