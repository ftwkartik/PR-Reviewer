from pathlib import Path

from app.indexing.chunker import chunk_file
from app.indexing.languages.python import PythonAdapter, module_name

FIXTURE = Path(__file__).parent.parent / "fixtures/sample_repo/app/auth/session.py"
SRC = FIXTURE.read_text()


def chunks():  # type: ignore[no-untyped-def]
    return {c.qualified_name: c for c in chunk_file("app/auth/session.py", "python", SRC, "blob1")}


def test_symbols_extracted() -> None:
    c = chunks()
    assert {"SessionManager", "SessionManager.__init__", "SessionManager.verify_token",
            "SessionManager.now", "current_user"} <= set(c)  # fmt: skip
    assert c["SessionManager.verify_token"].symbol_type == "method"
    assert c["SessionManager.verify_token"].parent_symbol == "SessionManager"
    assert c["current_user"].symbol_type == "function"


def test_line_ranges_point_at_real_source() -> None:
    lines = SRC.splitlines()
    for ch in chunk_file("app/auth/session.py", "python", SRC, "b"):
        assert ch.content == "\n".join(lines[ch.start_line - 1 : ch.end_line])


def test_method_chunk_contents_and_signature() -> None:
    m = chunks()["SessionManager.verify_token"]
    assert m.content.lstrip().startswith("async def verify_token")
    assert "raw: str" in (m.signature or "") and "Session | None" in (m.signature or "")
    assert (
        "decode_token" in m.called_names
        and "self.repo.get" in m.called_names
        and "get" in m.called_names
    )


def test_decorators_included_and_route_visible() -> None:
    f = chunks()["current_user"]
    assert f.content.startswith('@router.get("/me")')
    assert '@router.get("/me")' in (f.signature or "")


def test_class_chunk_is_header_only_with_method_index() -> None:
    c = chunks()["SessionManager"]
    assert "verify_token" in (c.signature or "") and "ttl = SESSION_TTL" in c.content
    assert "def verify_token" not in c.content
    assert c.bases == ["BaseManager"]


def test_relative_imports_resolved() -> None:
    m = next(
        c
        for c in chunk_file("app/auth/session.py", "python", SRC, "b")
        if c.symbol_type == "module"
    )
    assert "app.models.session.Session" in m.imports
    assert "app.auth.tokens" in m.imports or "app.auth.tokens.decode_token" in m.imports
    assert "app.auth.tokens.decode_token" in m.imports


def test_module_header_and_trailing_section() -> None:
    mods = [
        c
        for c in chunk_file("app/auth/session.py", "python", SRC, "b")
        if c.symbol_type == "module"
    ]
    assert len(mods) == 2 and mods[0].start_line == 1
    assert 'print("demo")' in mods[1].content


def test_test_flag() -> None:
    src = "def test_x():\n    assert True\n"
    assert all(c.is_test for c in chunk_file("tests/test_a.py", "python", src, "b"))
    assert not any(c.is_test for c in chunk_file("app/a.py", "python", "def f():\n    pass\n", "b"))


def test_oversized_function_split_with_contiguous_ranges() -> None:
    body = "\n".join(f"    x{i} = compute({i})  # " + "y" * 60 for i in range(80))
    src = f"def big(a):\n{body}\n    return a\n"
    parts = [c for c in chunk_file("app/big.py", "python", src, "b") if c.qualified_name == "big"]
    assert len(parts) > 1
    assert parts[0].start_line == 1 and parts[-1].end_line == len(src.splitlines())
    for a, b in zip(parts, parts[1:], strict=False):
        assert b.start_line == a.end_line + 1
    assert all("def big(a):" in (p.signature or "") for p in parts)


def test_syntax_error_falls_back_to_treesitter() -> None:
    src = "def ok(a):\n    return a\n\nclass Broken(:\n    pass\n\ndef also_ok():\n    return 1\n"
    names = {c.qualified_name for c in PythonAdapter().chunk("app/x.py", src)}
    assert {"ok", "also_ok"} <= names


def test_garbage_falls_back_to_windows() -> None:
    cs = PythonAdapter().chunk("x.py", "@@@@ not python @@@@\n" * 10)
    assert cs and cs[0].symbol_type == "fallback"


def test_module_name() -> None:
    assert module_name("app/auth/__init__.py") == "app.auth"
    assert module_name("app/auth/session.py") == "app.auth.session"


def test_embedding_text_is_header_enriched_but_content_verbatim() -> None:
    m = chunks()["SessionManager.verify_token"]
    assert m.embedding_text().startswith("# app/auth/session.py")
    assert "method SessionManager.verify_token" in m.embedding_text()
