"""Tiered context selection under a token budget (pure logic, no I/O)."""

from dataclasses import dataclass, field

from app.domain.retrieval import ContextBundle, DropInfo, RetrievedContext, Tier

DEFAULT_SHARES: dict[Tier, float] = {
    Tier.IMMEDIATE: 0.25,
    Tier.DEPENDENCY: 0.20,
    Tier.USAGE: 0.10,
    Tier.VALIDATION: 0.15,
    Tier.RAG: 0.20,
    Tier.CONVENTIONS: 0.05,
}


@dataclass
class Selection:
    bundle: ContextBundle = field(default_factory=ContextBundle)


def _contains(outer: RetrievedContext, inner: RetrievedContext) -> bool:
    a, b = outer.chunk, inner.chunk
    return (
        a.origin == b.origin and a.path == b.path
        and a.start_line <= b.start_line and b.end_line <= a.end_line
        and (a.start_line, a.end_line) != (b.start_line, b.end_line)
    )  # fmt: skip


def dedupe(items: list[RetrievedContext]) -> tuple[list[RetrievedContext], list[DropInfo]]:
    """Merge identical spans (best tier wins, reasons unioned); drop spans inside a larger one."""
    merged: dict[str, RetrievedContext] = {}
    drops: list[DropInfo] = []
    for it in items:
        cur = merged.get(it.key)
        if cur is None:
            merged[it.key] = it
            continue
        better, other = (it, cur) if it.tier < cur.tier else (cur, it)
        better.reasons = list(dict.fromkeys([*better.reasons, *other.reasons]))
        better.score = max(better.score, other.score)
        merged[it.key] = better
        drops.append(DropInfo(other.chunk.path, f"{other.chunk.start_line}-{other.chunk.end_line}",
                              other.tier, "duplicate"))  # fmt: skip
    kept = list(merged.values())
    final: list[RetrievedContext] = []
    for it in kept:
        outer = next((o for o in kept if _contains(o, it)), None)
        if outer is not None and outer.tier <= it.tier + 1:
            outer.reasons = list(dict.fromkeys([*outer.reasons, *it.reasons]))
            drops.append(DropInfo(it.chunk.path, f"{it.chunk.start_line}-{it.chunk.end_line}",
                                  it.tier, "contained"))  # fmt: skip
        else:
            final.append(it)
    return final, drops


def select_within_budget(
    candidates: list[RetrievedContext], budget: int, shares: dict[Tier, float] | None = None
) -> ContextBundle:
    """Fill tiers in priority order up to their share, then let leftover budget roll down.

    Tier 1 (the code under review) is always included; everything else competes for the rest.
    """
    shares = shares or DEFAULT_SHARES
    items, drops = dedupe(candidates)
    by_tier: dict[Tier, list[RetrievedContext]] = {t: [] for t in Tier}
    for it in items:
        by_tier[it.tier].append(it)
    for lst in by_tier.values():
        lst.sort(key=lambda i: (-i.score, i.chunk.path, i.chunk.start_line))

    chosen: list[RetrievedContext] = []
    used = 0

    def take(it: RetrievedContext) -> None:
        nonlocal used
        chosen.append(it)
        used += it.chunk.token_count

    for it in by_tier[Tier.IMMEDIATE]:  # never dropped for budget
        take(it)
    remaining = {t: [i for i in by_tier[t] if t != Tier.IMMEDIATE] for t in Tier}

    for tier in Tier:
        if tier == Tier.IMMEDIATE:
            continue
        cap = int(budget * shares.get(tier, 0.0))
        spent = 0
        rest: list[RetrievedContext] = []
        for it in remaining[tier]:
            t = it.chunk.token_count
            if spent + t <= cap and used + t <= budget:
                take(it)
                spent += t
            else:
                rest.append(it)
        remaining[tier] = rest
    for tier in Tier:  # roll unused budget down, still in priority order
        for it in remaining[tier]:
            t = it.chunk.token_count
            if used + t <= budget:
                take(it)
            else:
                drops.append(DropInfo(it.chunk.path, f"{it.chunk.start_line}-{it.chunk.end_line}",
                                      it.tier, "budget"))  # fmt: skip

    chosen.sort(key=lambda i: (i.tier, i.chunk.path, i.chunk.start_line))
    for n, it in enumerate(chosen, start=1):
        it.ctx_id = f"c{n}"
    return ContextBundle(items=chosen, tokens_used=used, budget=budget, dropped=drops)
