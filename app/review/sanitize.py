"""Sanitize model-written text before it is posted to GitHub.

Findings are derived from attacker-influenceable input (the PR itself), so even validated text
must not be able to ping people, embed trackers/HTML, or smuggle links.
"""

import re

_HTML = re.compile(
    r"</?(script|img|iframe|a|style|details|summary|svg|object|embed|form|input|link|meta|"
    r"div|span|table|tr|td|th|br|p|h[1-6]|button|video|audio|source)\b[^>]*>",
    re.IGNORECASE,
)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w`])@([A-Za-z0-9_-])")
_ISSUE_REF = re.compile(r"(?<![\w&])#(\d+)")
ZWSP = "​"


def sanitize_markdown(text: str) -> str:
    text = _MD_IMAGE.sub("", text)
    text = _HTML.sub("", text)
    text = _URL.sub(lambda m: m.group(0) if m.group(0).split("/")[2].endswith("github.com")
                    else "[link removed]", text)  # fmt: skip
    text = _MENTION.sub(lambda m: f"@{ZWSP}{m.group(1)}", text)
    text = _ISSUE_REF.sub(lambda m: f"#{ZWSP}{m.group(1)}", text)
    return text.strip()


def sanitize_inline(text: str) -> str:
    """Single-line variant for titles: also drops newlines and backticks."""
    return sanitize_markdown(" ".join(text.split())).replace("`", "'")
