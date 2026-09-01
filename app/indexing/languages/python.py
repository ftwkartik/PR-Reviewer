"""Python semantic chunker.

Primary: stdlib `ast` (exact symbols, signatures, imports, call names).
Fallback: tree-sitter, which is error-tolerant, for files `ast` cannot parse (syntax errors,
Python 2, half-written code). Last resort: line windows.
"""

import ast
from pathlib import PurePosixPath

from app.domain.code import CodeChunk
from app.github.classify import is_test_path
from app.retrieval.tokens import estimate_tokens

MAX_CHUNK_TOKENS = 800
MODULE_HEADER_MAX_LINES = 120
WINDOW_LINES = 60


def module_name(path: str) -> str:
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import(node: ast.AST, module: str, is_package: bool) -> list[str]:
    out: list[str] = []
    if isinstance(node, ast.Import):
        out += [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:
            pkg = module.split(".") if is_package else module.split(".")[:-1]
            pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
            base = ".".join([*pkg, *([base] if base else [])])
        out += [f"{base}.{a.name}" if base else a.name for a in node.names]
    return out


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _called_names(node: ast.AST) -> list[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            dotted = _dotted(sub.func)
            if dotted:
                names.add(dotted)
                names.add(dotted.rsplit(".", 1)[-1])  # short form for caller lookups
    return sorted(names)


class PythonAdapter:
    language = "python"

    def chunk(self, path: str, source: str) -> list[CodeChunk]:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return _treesitter_fallback(path, source) or _windows(path, source)
        return _AstChunker(path, source, tree).run()


class _AstChunker:
    def __init__(self, path: str, source: str, tree: ast.Module) -> None:
        self.path, self.tree = path, tree
        self.lines = source.splitlines()
        self.module = module_name(path)
        self.is_package = PurePosixPath(path).name == "__init__.py"
        self.test_file = is_test_path(path)
        self.imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import | ast.ImportFrom):
                self.imports += _resolve_import(node, self.module, self.is_package)
        self.chunks: list[CodeChunk] = []

    def text(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1 : end])

    @staticmethod
    def _start(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
        return min([node.lineno, *[d.lineno for d in node.decorator_list]])

    def run(self) -> list[CodeChunk]:
        run_start: int | None = None
        run_end = 0
        first_run = True
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = node.lineno
                if run_start is None:
                    run_start = start
                run_end = node.end_lineno or start
                continue
            if run_start is not None:
                self._module_section(run_start, run_end, header=first_run)
                first_run, run_start = False, None
            if isinstance(node, ast.ClassDef):
                self._class(node)
            else:
                self._function(node, parent=None)
        if run_start is not None:
            self._module_section(run_start, run_end, header=first_run)
        return self.chunks

    def _module_section(self, start: int, end: int, *, header: bool) -> None:
        if header:
            end = min(end, start + MODULE_HEADER_MAX_LINES - 1)
        content = self.text(start, end)
        if not content.strip():
            return
        self.chunks.append(
            CodeChunk(
                path=self.path,
                language="python",
                symbol_type="module",
                start_line=start,
                end_line=end,
                content=content,
                symbol="<module>",
                qualified_name=f"<module:{self.module}>",
                imports=self.imports if header else [],
                is_test=self.test_file,
                docstring=ast.get_docstring(self.tree) if header else None,
            )  # fmt: skip
        )

    def _signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
        first_body = node.body[0].lineno if node.body else node.lineno
        start = self._start(node)
        end = max(node.lineno, first_body - 1) if first_body > node.lineno else node.lineno
        sig = " ".join(ln.strip() for ln in self.lines[start - 1 : end])
        return sig[:400]

    def _class(self, node: ast.ClassDef) -> None:
        methods = [n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        start = self._start(node)
        header_end = (self._start(methods[0]) - 1) if methods else (node.end_lineno or node.lineno)
        header_end = max(header_end, node.lineno)
        sigs = "; ".join(self._signature(m) for m in methods)
        self.chunks.append(
            CodeChunk(
                path=self.path,
                language="python",
                symbol_type="class",
                start_line=start,
                end_line=header_end,
                content=self.text(start, header_end),
                symbol=node.name,
                qualified_name=node.name,
                signature=f"{self._signature(node)}  [methods: {sigs}]"[:1200]
                if sigs
                else self._signature(node),
                imports=self.imports,
                bases=[ast.unparse(b) for b in node.bases],
                is_test=self.test_file or node.name.startswith("Test"),
                docstring=ast.get_docstring(node),
                called_names=_called_names(node) if not methods else [],
            )  # fmt: skip
        )
        for m in methods:
            self._function(m, parent=node.name)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, parent: str | None) -> None:
        start, end = self._start(node), node.end_lineno or node.lineno
        qname = f"{parent}.{node.name}" if parent else node.name
        common = dict(
            path=self.path, language="python", symbol_type="method" if parent else "function",
            symbol=node.name, qualified_name=qname, parent_symbol=parent,
            signature=self._signature(node), imports=self.imports,
            called_names=_called_names(node), docstring=ast.get_docstring(node),
            is_test=self.test_file or node.name.startswith("test_"),
        )  # fmt: skip
        content = self.text(start, end)
        if estimate_tokens(content) <= MAX_CHUNK_TOKENS:
            self.chunks.append(CodeChunk(start_line=start, end_line=end, content=content, **common))  # type: ignore[arg-type]
            return
        # Oversized: split at top-level statement boundaries, repeating the signature in metadata.
        part_start = start
        for i, stmt in enumerate(node.body):
            stmt_end = stmt.end_lineno or stmt.lineno
            text = self.text(part_start, stmt_end)
            last = i == len(node.body) - 1
            nxt = node.body[i + 1] if not last else None
            nxt_end = (nxt.end_lineno or nxt.lineno) if nxt else stmt_end
            if last or estimate_tokens(self.text(part_start, nxt_end)) > MAX_CHUNK_TOKENS:
                self.chunks.append(
                    CodeChunk(start_line=part_start, end_line=stmt_end, content=text, **common)  # type: ignore[arg-type]
                )
                part_start = stmt_end + 1


def _windows(path: str, source: str) -> list[CodeChunk]:
    lines = source.splitlines()
    out: list[CodeChunk] = []
    for i in range(0, len(lines), WINDOW_LINES):
        block = lines[i : i + WINDOW_LINES]
        if "".join(block).strip():
            out.append(
                CodeChunk(
                    path=path,
                    language="python",
                    symbol_type="fallback",
                    start_line=i + 1,
                    end_line=i + len(block),
                    content="\n".join(block),
                    is_test=is_test_path(path),
                )  # fmt: skip
            )
    return out


def _treesitter_fallback(path: str, source: str) -> list[CodeChunk]:
    """Error-tolerant structural chunking of files the stdlib parser rejects."""
    try:
        import tree_sitter_python as tsp
        from tree_sitter import Language, Parser

        parser = Parser(Language(tsp.language()))
        data = source.encode()
        root = parser.parse(data).root_node
    except Exception:
        return []
    chunks: list[CodeChunk] = []
    for node in root.children:
        target = node
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is None:
                continue
            target = inner
        if target.type not in {"function_definition", "class_definition"}:
            continue
        name_node = target.child_by_field_name("name")
        if name_node is None or name_node.text is None:
            continue
        name = name_node.text.decode()
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        chunks.append(
            CodeChunk(
                path=path,
                language="python",
                symbol_type="class" if target.type == "class_definition" else "function",
                start_line=start,
                end_line=end,
                content="\n".join(source.splitlines()[start - 1 : end]),
                symbol=name,
                qualified_name=name,
                is_test=is_test_path(path) or name.startswith("test_"),
            )  # fmt: skip
        )
    return chunks
