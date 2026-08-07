import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults_and_secret_not_leaked() -> None:
    s = Settings(_env_file=None, github_webhook_secret="hunter2")  # type: ignore[call-arg]
    assert s.review_confidence_threshold == 0.75
    assert "hunter2" not in repr(s)


def test_threshold_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, review_confidence_threshold=1.5)  # type: ignore[call-arg]
