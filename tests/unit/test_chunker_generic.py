from app.indexing.chunker import chunk_file


def test_markdown_split_by_heading() -> None:
    src = "# Title\nintro\n\n## Setup\nrun it\n\n## Testing\nuse pytest\n"
    cs = chunk_file("README.md", "markdown", src, "b")
    assert [c.symbol for c in cs] == ["Title", "Setup", "Testing"]
    assert all(c.symbol_type == "doc" for c in cs)


def test_yaml_split_by_top_level_key() -> None:
    src = "services:\n  api:\n    image: x\nvolumes:\n  data: {}\n"
    cs = chunk_file("docker-compose.yml", "yaml", src, "b")
    assert [c.symbol for c in cs] == ["services", "volumes"]


def test_toml_tables() -> None:
    cs = chunk_file(
        "pyproject.toml", "toml", '[project]\nname="x"\n\n[tool.ruff]\nline-length=100\n', "b"
    )
    assert [c.symbol for c in cs] == ["project", "tool.ruff"]


def test_long_dockerfile_windowed() -> None:
    src = "\n".join(f"RUN echo {i}" for i in range(200))
    cs = chunk_file("Dockerfile", "dockerfile", src, "b")
    assert len(cs) == 3 and cs[0].start_line == 1 and cs[-1].end_line == 200
