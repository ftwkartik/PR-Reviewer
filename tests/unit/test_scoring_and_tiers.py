from app.domain.code import CodeChunk
from app.domain.retrieval import RetrievedContext, Tier
from app.retrieval.refs import all_candidate_paths, resolve_imports
from app.retrieval.scoring import ScoringContext, rrf, score_candidates
from app.retrieval.store import to_tsquery
from app.retrieval.tiers import select_within_budget


def chunk(
    path: str,
    name: str,
    start: int = 1,
    end: int = 10,
    content: str = "x" * 400,
    cid: str = "",
    **kw,
) -> CodeChunk:  # type: ignore[no-untyped-def]
    return CodeChunk(path=path, language="python", symbol_type=kw.pop("symbol_type", "function"),
                     start_line=start, end_line=end, content=content, symbol=name.split(".")[-1],
                     qualified_name=name, chunk_id=cid or f"{path}:{start}", **kw)  # fmt: skip


def test_exact_symbol_beats_semantically_similar_decoy() -> None:
    exact = chunk("a/session.py", "SessionManager.verify_token", cid="exact")
    decoy = chunk("b/billing.py", "verify_payment_token", cid="decoy")
    ranked = {"vector": [decoy, exact], "lexical": [decoy, exact]}  # retrievers prefer the decoy
    ctx = ScoringContext(referenced_names=frozenset({"SessionManager.verify_token"}))
    out = score_candidates(ranked, ctx)
    assert out[0].chunk.chunk_id == "exact" and "exact_symbol" in out[0].reasons


def test_rrf_rewards_agreement_between_retrievers() -> None:
    a, b, c = chunk("a.py", "a", cid="a"), chunk("b.py", "b", cid="b"), chunk("c.py", "c", cid="c")
    fused = rrf({"lexical": [a, b], "vector": [c, a]})
    assert fused["a"][0] > fused["b"][0] and fused["a"][0] > fused["c"][0]


def test_boosts_and_penalties() -> None:
    near = chunk("app/auth/x.py", "helper", cid="near")
    far = chunk("other/y.py", "helper2", cid="far")
    dunder = chunk("app/auth/z.py", "__init__", cid="init", content="x" * 400)
    ctx = ScoringContext(
        changed_dirs=frozenset({"app/auth"}), imported_paths=frozenset({"app/auth/x.py"})
    )
    out = {s.chunk.chunk_id: s for s in score_candidates({"lexical": [far, near, dunder]}, ctx)}
    assert out["near"].score > out["far"].score
    assert "generic" in out["init"].reasons and "same_dir" in out["init"].reasons


def test_scoring_is_deterministic_on_ties() -> None:
    cs = [chunk(f"p{i}.py", f"f{i}", cid=f"c{i}") for i in range(5)]
    a = [s.chunk.chunk_id for s in score_candidates({"lexical": cs}, ScoringContext())]
    b = [s.chunk.chunk_id for s in score_candidates({"lexical": list(cs)}, ScoringContext())]
    assert a == b


def ri(c: CodeChunk, tier: Tier, score: float = 1.0) -> RetrievedContext:
    return RetrievedContext(c, tier, score, [])


def test_budget_always_keeps_tier1_and_rolls_down() -> None:
    t1 = ri(chunk("a.py", "changed", 1, 40, "x" * 4000), Tier.IMMEDIATE)
    deps = [
        ri(chunk("d.py", f"d{i}", i * 10, i * 10 + 5, "y" * 400), Tier.DEPENDENCY, 10 - i)
        for i in range(6)
    ]
    bundle = select_within_budget([t1, *deps], budget=1500)
    assert t1 in bundle.items  # 1000 tokens, tier 1 never dropped
    assert bundle.tokens_used <= 1500
    kept = [i for i in bundle.items if i.tier == Tier.DEPENDENCY]
    assert [i.chunk.symbol for i in kept][:2] == ["d0", "d1"]  # best-scored first
    assert any(d.reason == "budget" for d in bundle.dropped)


def test_duplicates_merged_and_contained_dropped() -> None:
    method = chunk("a.py", "C.m", 5, 10, cid="m")
    same = chunk("a.py", "C.m", 5, 10, cid="m2")
    cls = chunk("a.py", "C", 1, 20, cid="cls")
    b = select_within_budget(
        [ri(method, Tier.RAG), ri(same, Tier.IMMEDIATE), ri(cls, Tier.IMMEDIATE)], 10_000
    )
    assert [i.chunk.chunk_id for i in b.items] == ["cls"]  # merged, then contained in the class
    reasons = {d.reason for d in b.dropped}
    assert "duplicate" in reasons


def test_share_caps_prevent_one_tier_from_hogging_budget() -> None:
    rag = [
        ri(chunk("r.py", f"r{i}", i * 10, i * 10 + 5, "z" * 400), Tier.RAG, 5) for i in range(10)
    ]
    usage = [ri(chunk("u.py", "caller", 1, 5, "q" * 400), Tier.USAGE, 1)]
    bundle = select_within_budget([*rag, *usage], budget=1000)
    assert any(
        i.tier == Tier.USAGE for i in bundle.items
    )  # low-scored but higher-priority tier survives


def test_import_resolution_prefers_longest_existing_module() -> None:
    existing = {"app/auth/tokens.py", "app/models/__init__.py"}
    t = resolve_imports(["app.auth.tokens.decode_token", "app.models.Session", "os.path"], existing)
    assert [(x.path, x.symbol) for x in t] == [
        ("app/auth/tokens.py", "decode_token"),
        ("app/models/__init__.py", "Session"),
    ]
    assert "src/app/auth/tokens.py" in all_candidate_paths(["app.auth.tokens"])


def test_to_tsquery() -> None:
    q = to_tsquery("SessionManager.verify_token(self, raw)")
    assert q and "sessionmanager" in q and "verify" in q and "token" in q and "self" not in q
    assert to_tsquery("a b ; DROP TABLE x --") is not None and ";" not in (to_tsquery("a; b") or "")
    assert to_tsquery("()") is None
