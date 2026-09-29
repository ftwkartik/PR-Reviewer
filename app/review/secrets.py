"""Redact credentials before repository text is sent to a model provider.

Private repositories contain secrets by accident. Detection is regex-based and deliberately
conservative (few false positives) -- it reduces leakage risk, it does not eliminate it. The
same function is applied to the prompt text and to the validator's evidence corpus, so a model
quoting a redacted line still matches.
"""

import re
from collections.abc import Callable

from app.observability.metrics import REDACTIONS

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("api_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("base64_blob", re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/=])")),
]
# name = "value": keep the name (context for the reviewer), drop the value
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:api[_-]?key|secret|passwd|password|token|credential|private[_-]?key)[\w.-]*
     \s*[:=]\s*)
    (["'])([^"'\s]{12,})\2
    """
)


def _replacer(kind: str, hit: Callable[[str], str]) -> Callable[[re.Match[str]], str]:
    def repl(_: re.Match[str]) -> str:
        return hit(kind)

    return repl


def redact(text: str, on_redact: Callable[[str], None] | None = None) -> str:
    def hit(kind: str) -> str:
        REDACTIONS.labels(kind).inc()
        if on_redact:
            on_redact(kind)
        return f"[REDACTED:{kind}]"

    for kind, pat in _PATTERNS:
        text = pat.sub(_replacer(kind, hit), text)
    return _ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{hit('secret_value')}{m.group(2)}", text
    )
