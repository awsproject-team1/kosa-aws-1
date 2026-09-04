"""Single public HTTP error mapping for Backend failures."""

from dataclasses import dataclass

from apps.backend.auth import AuthorizationDenied, InvalidIdentityClaims
from apps.backend.deployment.approval import DeploymentApprovalError, DeploymentConflictError
from apps.backend.jobs.lifecycle import InvalidJobTransition, StaleJobRevision
from apps.backend.remediation.service import RemediationNotAutomatableError
from apps.backend.repositories.ports import (
    DuplicateJobError,
    InvalidJobMutationError,
    RepositoryError,
    RevisionConflictError,
)
from packages.common.errors import PolicySourceDeleteForbidden, PolicySourceNotFound
from packages.contracts import ApiError


class AssessmentScopeDenied(PermissionError):
    """Raised when an approved assessment selector is outside JWT scope."""


class JobNotFoundError(LookupError):
    """Raised when a customer-scoped Job record does not exist."""


class RequestValidationError(ValueError):
    """Raised only for malformed public request fields or JSON."""


class WorkflowDispatchError(RuntimeError):
    """Raised after a persisted Job is compensated following dispatch failure."""


@dataclass(frozen=True, slots=True)
class PublicFailure:
    """HTTP status and public-safe error selected from one trusted mapping."""

    status_code: int
    error: ApiError


def sanitize_public_error(error: BaseException) -> ApiError:
    """Return only the approved public detail for a trusted failure category."""
    return sanitize_public_failure(error).error


def sanitize_public_failure(error: BaseException) -> PublicFailure:
    """Map an internal exception to the documented HTTP status and error code."""
    if not isinstance(error, BaseException):
        raise TypeError("error must be an exception")

    if isinstance(error, InvalidIdentityClaims):
        return _failure(401, "UNAUTHORIZED", "Authentication is required")
    if isinstance(error, (AuthorizationDenied, AssessmentScopeDenied)):
        return _failure(403, "SCOPE_DENIED", "The requested resource is outside the approved scope")
    if isinstance(error, (JobNotFoundError, PolicySourceNotFound)):
        return _failure(404, "NOT_FOUND", "The requested resource was not found")
    if isinstance(error, RequestValidationError):
        return _failure(400, "VALIDATION_ERROR", "The request is invalid")
    if isinstance(
        error,
        (
            DeploymentApprovalError,
            DeploymentConflictError,
            PolicySourceDeleteForbidden,
            RemediationNotAutomatableError,
        ),
    ):
        return _failure(409, "CONFLICT", "The request conflicts with current state")
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
        return _failure(409, "CONFLICT", "The request conflicts with current state")
    if isinstance(error, (RepositoryError, WorkflowDispatchError)):
        return _failure(503, "EXECUTION_ERROR", "The service is temporarily unavailable")
    return _failure(500, "EXECUTION_ERROR", "An internal error occurred")


def _failure(status_code: int, code: str, message: str) -> PublicFailure:
    return PublicFailure(status_code=status_code, error=ApiError(code=code, message=message))
