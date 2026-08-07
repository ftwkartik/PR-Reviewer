import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9_\-.=]{8,}"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9\-_]{16,})\b"),
]
_SENSITIVE_KEYS = {"authorization", "private_key", "api_key", "secret", "token", "password"}


def redact(value: str) -> str:
    for pat in _SECRET_PATTERNS:
        value = pat.sub("[REDACTED]", value)
    return value


def _redact_processor(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, val in list(event.items()):
        if key.lower() in _SENSITIVE_KEYS:
            event[key] = "[REDACTED]"
        elif isinstance(val, str):
            event[key] = redact(val)
    return event


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
    )


def bind_review_context(**ids: str | int | None) -> None:
    """Bind review_id, repository, pull_number, delivery_id, job_id, commit_sha, ..."""
    structlog.contextvars.bind_contextvars(**{k: v for k, v in ids.items() if v is not None})
