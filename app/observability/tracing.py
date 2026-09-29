"""Tracing hook. No-op unless OpenTelemetry is installed and configured, so the core has no hard
dependency on it. Call sites use `span()`; swapping in a real tracer only changes this file."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    try:
        from opentelemetry import trace  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        yield
        return
    with trace.get_tracer("pr-review-agent").start_as_current_span(name) as s:
        for k, v in attributes.items():
            s.set_attribute(k, v)
        yield
