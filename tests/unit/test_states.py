import pytest

from app.domain.states import (
    ALLOWED,
    TERMINAL,
    InvalidTransitionError,
    ReviewStatus,
    check_transition,
)


def test_happy_path() -> None:
    path = ["QUEUED", "FETCHING_PR", "INDEXING", "RETRIEVING_CONTEXT", "ANALYZING",
            "VALIDATING", "PUBLISHING", "COMPLETED"]  # fmt: skip
    for a, b in zip(path, path[1:], strict=False):
        check_transition(ReviewStatus(a), ReviewStatus(b))


def test_terminal_states_are_final() -> None:
    for s in TERMINAL:
        assert not ALLOWED[s]


def test_invalid_transition() -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(ReviewStatus.QUEUED, ReviewStatus.PUBLISHING)
