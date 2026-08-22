import io
import tarfile
from pathlib import Path

import pytest

from app.core.errors import PermanentError
from app.github.tarball import safe_extract


def make_tar(path: Path, entries: list[tuple[str, bytes | None, str]]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data or b"")
                tar.addfile(info, io.BytesIO(data or b""))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)


def test_extracts_and_strips_top_dir(tmp_path: Path) -> None:
    make_tar(tmp_path / "a.tgz", [("o-r-sha/app/x.py", b"print(1)", "file")])
    out = safe_extract(tmp_path / "a.tgz", tmp_path / "out")
    assert out == ["app/x.py"] and (tmp_path / "out/app/x.py").read_bytes() == b"print(1)"


def test_path_traversal_rejected(tmp_path: Path) -> None:
    make_tar(tmp_path / "a.tgz", [("o-r-sha/../../evil.py", b"x", "file")])
    with pytest.raises(PermanentError):
        safe_extract(tmp_path / "a.tgz", tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_symlinks_never_materialized(tmp_path: Path) -> None:
    make_tar(
        tmp_path / "a.tgz", [("o-r-sha/link", None, "symlink"), ("o-r-sha/ok.py", b"1", "file")]
    )
    out = safe_extract(tmp_path / "a.tgz", tmp_path / "out")
    assert out == ["ok.py"] and not (tmp_path / "out/link").exists()


def test_limits_enforced(tmp_path: Path) -> None:
    make_tar(tmp_path / "a.tgz", [(f"o-r-sha/f{i}.py", b"x", "file") for i in range(5)])
    with pytest.raises(PermanentError):
        safe_extract(tmp_path / "a.tgz", tmp_path / "out", max_files=3)


def test_oversized_single_file_skipped(tmp_path: Path) -> None:
    make_tar(
        tmp_path / "a.tgz",
        [("o-r-sha/big.bin", b"x" * 100, "file"), ("o-r-sha/s.py", b"1", "file")],
    )
    assert safe_extract(tmp_path / "a.tgz", tmp_path / "out", max_file_bytes=10) == ["s.py"]
