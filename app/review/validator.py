"""Finding validation: the model proposes, the application decides.

Pipeline (each failure records a reason that is persisted for evaluation):
  schema (Pydantic, upstream) -> file exists in PR -> lines inside one diff hunk (snap/trim)
  -> evidence quote really exists in the diff/context -> context refs exist (else downgrade)
  -> replacement code sane -> sanitize text -> confidence gate -> severity policy
Dedup and per-file/per-PR caps are applied afterwards (deduplicator.py).
"""

import ast
import hashlib
import re
import textwrap
from dataclasses import dataclass, field

from app.domain.pr import ChangedFile, DiffHunk
from app.domain.retrieval import ContextBundle
from app.domain.review import ProcessedFinding, ReviewFinding
from app.review.injection import strip_injections
from app.review.sanitize import sanitize_inline, sanitize_markdown
from app.review.secrets import redact

MIN_EVIDENCE_CHARS = 6
FUZZY_LINE_MATCH = 0.9
MAX_REPLACEMENT_LINES = 40
CTX_REF_PENALTY = 0.7

_WS = re.compile(r"\s+")
_RENDER_PREFIX = re.compile(r"^\s*(?:\d+\s*[+ ]\s*|-\d+\s*)\|\s?")


def collapse(s: str) -> str:
    return _WS.sub(" ", s).strip()


def strip_render_prefix(line: str) -> str:
    """Models sometimes copy the printed line-number gutter into their quote; drop it."""
    return _RENDER_PREFIX.sub("", line)


_CODE_TOKEN = re.compile(r"[A-Za-z0-9_]+")
MIN_TOKEN_QUOTE = 3  # token-level matching needs at least this many tokens...
MIN_TOKEN_CHARS = 12  # ...and this many characters, so trivial quotes cannot match by chance


_STRING_PREFIXES = frozenset({"f", "r", "b", "u", "fr", "rf", "br", "rb"})


def code_tokens(s: str) -> list[str]:
    """Identifiers/numbers, minus bare string prefixes (f"..." etc.), applied to both sides."""
    return [t for t in _CODE_TOKEN.findall(s) if t.lower() not in _STRING_PREFIXES]


@dataclass
class Corpus:
    """Everything the model was actually shown, normalised for verbatim-quote checks."""

    lines: set[str] = field(default_factory=set)
    blob: str = ""
    tokens: str = ""  # identifiers/numbers of the corpus joined by single spaces

    @classmethod
    def build(cls, files: list[ChangedFile], bundle: ContextBundle | None) -> "Corpus":
        texts: list[str] = []
        for f in files:
            texts += [strip_injections(redact(ln.text)) for h in f.hunks for ln in h.lines]
        if bundle:
            for item in bundle.items:
                texts += strip_injections(redact(item.chunk.content)).splitlines()
        collapsed = [c for c in (collapse(t) for t in texts) if c]
        tokens = " ".join(code_tokens(" ".join(collapsed)))
        return cls(set(collapsed), " ".join(collapsed), " " + tokens)


def evidence_exists(quote: str, corpus: Corpus) -> bool:
    qlines = [collapse(strip_render_prefix(ln)) for ln in redact(quote).splitlines() if ln.strip()]
    qlines = [q for q in qlines if q]
    if not qlines or len(" ".join(qlines)) < MIN_EVIDENCE_CHARS:
        return False
    joined = " ".join(qlines)
    if joined in corpus.blob:
        return True
    if len(qlines) > 1:
        hits = sum(1 for q in qlines if q in corpus.lines or q in corpus.blob)
        if hits / len(qlines) >= FUZZY_LINE_MATCH:
            return True
    return _token_match(joined, corpus)


TOKEN_COVERAGE = 0.8  # share of the quote's tokens that must appear consecutively in the corpus


def _token_match(quote: str, corpus: Corpus) -> bool:
    """Tolerant tier: most of the quote's code tokens appear consecutively, in order, in what the
    model was shown. Absorbs quote/escape/punctuation drift, a truncated last token, and a few
    stray leading/trailing tokens (typical of small local models) while rejecting invented code.
    """
    toks = code_tokens(quote)
    if len(toks) < MIN_TOKEN_QUOTE or sum(len(t) for t in toks) < MIN_TOKEN_CHARS:
        return False
    max_trim = int(len(toks) * (1 - TOKEN_COVERAGE))
    for lead in range(max_trim + 1):
        for trail in range(max_trim - lead + 1):
            core = toks[lead : len(toks) - trail]
            if len(core) < MIN_TOKEN_QUOTE or sum(len(t) for t in core) < MIN_TOKEN_CHARS:
                continue
            head = " ".join(re.escape(t) for t in core[:-1])
            pattern = " " + (head + " " if head else "") + re.escape(core[-1])  # last may be cut
            if re.search(pattern, corpus.tokens):
                return True
    return False


def normalize_path(path: str, files: dict[str, ChangedFile]) -> str | None:
    p = path.strip().strip("`'\"")
    candidates = [p, p.removeprefix("./"), p.removeprefix("a/"), p.removeprefix("b/")]
    return next((c for c in candidates if c in files), None)


def _valid_set(h: DiffHunk, side: str) -> frozenset[int]:
    return h.right_lines if side == "RIGHT" else h.deleted_lines


def resolve_lines(
    f: ChangedFile, start: int, end: int, side: str
) -> tuple[int, int, DiffHunk] | None:
    """Snap a model-proposed range onto lines that really exist in one hunk of the diff.

    Returns the (possibly trimmed) range and its hunk, or None if no line of the range is
    commentable. A range straddling hunks is trimmed to the hunk holding its anchor line.
    """
    for anchor in (start, end):
        hunk = f.hunk_containing(anchor, side)
        if hunk is None:
            continue
        valid = _valid_set(hunk, side)
        lo = hi = anchor
        while lo - 1 >= start and (lo - 1) in valid:
            lo -= 1
        while hi + 1 <= end and (hi + 1) in valid:
            hi += 1
        return lo, hi, hunk
    return None


def replacement_ok(code: str, file: ChangedFile, lo: int, hi: int, side: str) -> bool:
    if side != "RIGHT" or not code.strip() or "```" in code:
        return False
    if len(code.splitlines()) > MAX_REPLACEMENT_LINES:
        return False
    if not set(range(lo, hi + 1)) <= file.added_lines():
        return False  # a suggestion may only replace lines this PR added
    if file.language == "python":
        body = textwrap.dedent(code)
        for candidate in (body, "if True:\n" + textwrap.indent(body, "    ")):
            try:
                ast.parse(candidate)
                return True
            except SyntaxError:
                continue
        return False
    return True


def fingerprint(pf: ReviewFinding, path: str, lo: int) -> str:
    toks = sorted(set(re.findall(r"[a-z0-9]+", pf.title.lower())))[:6]
    raw = f"{path}|{pf.category}|{lo // 4}|{' '.join(toks)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ValidationSettings:
    confidence_threshold: float = 0.75
    report_low_severity: bool = False


def validate_findings(
    raw: list[tuple[int, ReviewFinding]],
    files_by_batch: dict[int, list[ChangedFile]],
    bundles: dict[int, ContextBundle | None],
    settings: ValidationSettings,
) -> list[ProcessedFinding]:
    """Validate each finding against ONLY what its own batch's model call was shown."""
    corpora: dict[int, Corpus] = {}
    out: list[ProcessedFinding] = []
    for batch_index, finding in raw:
        pf = ProcessedFinding(finding=finding, batch_index=batch_index)
        files = files_by_batch.get(batch_index, [])
        bundle = bundles.get(batch_index)
        if batch_index not in corpora:
            corpora[batch_index] = Corpus.build(files, bundle)
        by_path: dict[str, ChangedFile] = {}
        for f in files:  # a file split across batches appears once per batch with its own hunks
            by_path[f.path] = f
        _validate_one(pf, by_path, bundle, corpora[batch_index], settings)
        out.append(pf)
    return out


def _reject(pf: ProcessedFinding, reason: str) -> None:
    pf.status, pf.reject_reason = "rejected", reason


def _validate_one(
    pf: ProcessedFinding,
    files: dict[str, ChangedFile],
    bundle: ContextBundle | None,
    corpus: Corpus,
    settings: ValidationSettings,
) -> None:
    f = pf.finding
    path = normalize_path(f.path, files)
    if path is None:
        return _reject(pf, "unknown_file")
    cf = files[path]
    resolved = resolve_lines(cf, f.line_start, f.line_end, f.side)
    if resolved is None:
        return _reject(pf, "line_not_in_diff")
    lo, hi, _ = resolved
    if (lo, hi) != (f.line_start, f.line_end):
        pf.notes.append(f"lines adjusted {f.line_start}-{f.line_end} -> {lo}-{hi}")
    if not evidence_exists(f.evidence_quote, corpus):
        return _reject(pf, "evidence_not_found")

    known = bundle.by_id() if bundle else {}
    unknown = [r for r in f.context_refs if r not in known]
    if unknown:
        pf.confidence = round(pf.confidence * CTX_REF_PENALTY, 4)
        pf.notes.append(f"unknown context refs downgraded confidence: {unknown}")

    if pf.replacement_code is not None and not replacement_ok(
        pf.replacement_code, cf, lo, hi, f.side
    ):
        pf.notes.append("replacement_code dropped (failed validation)")
        pf.replacement_code = None

    # Persist the validated/snapped location in a copy so downstream code never sees raw numbers.
    pf.finding = f.model_copy(update={
        "path": path, "line_start": lo, "line_end": hi,
        "title": sanitize_inline(f.title)[:120],
        "explanation": sanitize_markdown(f.explanation),
        "suggested_fix": sanitize_markdown(f.suggested_fix),
        "context_refs": [r for r in f.context_refs if r in known],
    })  # fmt: skip
    if pf.replacement_code is not None:
        pf.replacement_code = pf.replacement_code.rstrip("\n")
    pf.fingerprint = fingerprint(pf.finding, path, lo)

    threshold = settings.confidence_threshold
    if f.severity in ("critical", "high"):
        threshold = max(0.0, threshold - 0.05)
    if pf.confidence < threshold:
        pf.status, pf.reject_reason = (
            "below_threshold",
            f"confidence {pf.confidence:.2f} < {threshold:.2f}",
        )
        return
    if f.severity == "low" and not settings.report_low_severity:
        pf.status, pf.reject_reason = "low_severity", "low severity is not posted inline"
