class ReviewError(Exception):
    """Base for pipeline errors. `code` is persisted on the job as error_code."""

    code = "internal_error"
    retryable = False


class TransientError(ReviewError):
    """Temporary failure (network, 5xx, rate limit): safe to retry the task."""

    code = "transient_error"
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(ReviewError):
    def __init__(self, message: str, *, code: str = "permanent_error") -> None:
        super().__init__(message)
        self.code = code


class StaleHeadError(ReviewError):
    code = "stale_head"


class CancelledError(ReviewError):
    code = "cancelled"
