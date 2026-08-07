from enum import StrEnum


class ReviewStatus(StrEnum):
    QUEUED = "QUEUED"
    FETCHING_PR = "FETCHING_PR"
    INDEXING = "INDEXING"
    RETRIEVING_CONTEXT = "RETRIEVING_CONTEXT"
    ANALYZING = "ANALYZING"
    VALIDATING = "VALIDATING"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


S = ReviewStatus
TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED, S.STALE})

ALLOWED: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    S.QUEUED: frozenset({S.FETCHING_PR, S.CANCELLED, S.FAILED}),
    S.FETCHING_PR: frozenset({S.INDEXING, S.STALE, S.CANCELLED, S.FAILED}),
    S.INDEXING: frozenset({S.RETRIEVING_CONTEXT, S.CANCELLED, S.FAILED}),
    S.RETRIEVING_CONTEXT: frozenset({S.ANALYZING, S.CANCELLED, S.FAILED}),
    S.ANALYZING: frozenset({S.VALIDATING, S.PARTIAL, S.CANCELLED, S.FAILED}),
    S.PARTIAL: frozenset({S.VALIDATING, S.FAILED}),
    S.VALIDATING: frozenset({S.PUBLISHING, S.CANCELLED, S.FAILED}),
    S.PUBLISHING: frozenset({S.COMPLETED, S.STALE, S.FAILED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.STALE: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


def check_transition(current: ReviewStatus, new: ReviewStatus) -> None:
    if new not in ALLOWED[current]:
        raise InvalidTransitionError(f"{current} -> {new} is not allowed")
