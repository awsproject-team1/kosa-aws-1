"""Stable public error projection for internal Backend failures."""

from apps.backend.jobs.lifecycle import InvalidJobTransition, StaleJobRevision
from apps.backend.repositories.ports import (
    DuplicateJobError,
    InvalidJobMutationError,
    RepositoryError,
    RevisionConflictError,
)
from packages.contracts import ApiError


def sanitize_public_error(error: BaseException) -> ApiError:
    """Map a trusted failure category without copying exception details."""
    if not isinstance(error, BaseException):
        raise TypeError("error must be an exception")

    if isinstance(
        error,
        (
            DuplicateJobError,
            InvalidJobMutationError,
            InvalidJobTransition,
            RevisionConflictError,
            StaleJobRevision,
        ),
    ):
        return ApiError(
            code="INVALID_STATE",
            message="Job state does not allow this operation",
        )
    if isinstance(error, RepositoryError):
        return ApiError(
            code="EXTERNAL_SERVICE_ERROR",
            message="A storage service request failed",
        )
    return ApiError(code="INTERNAL_ERROR", message="An internal error occurred")
