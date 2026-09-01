"""A-owned customer remediation-exception registration boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from packages.contracts import RemediationException, RemediationExceptionReason


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationExceptionRequest:
    """The complete client-controlled exception registration shape."""

    rule_id: str
    rule_version: str
    reason: RemediationExceptionReason
    expires_at: str
    resource_id: str | None = None
    ticket_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("rule_id", "rule_version", "expires_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.reason, RemediationExceptionReason):
            raise TypeError("reason must be a RemediationExceptionReason")
        for name in ("resource_id", "ticket_reference"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")


class RemediationExceptionRepository(Protocol):
    def create_exception(self, exception: RemediationException) -> None: ...


class RemediationExceptionApiService:
    """Register one approved, expiring exception in the JWT-derived customer scope."""

    def __init__(
        self,
        *,
        repository: RemediationExceptionRepository,
        exception_id_factory: Callable[[], str],
        now: Callable[[], datetime],
    ) -> None:
        if repository is None:
            raise TypeError("repository is required")
        if not callable(exception_id_factory) or not callable(now):
            raise TypeError("exception_id_factory and now must be callable")
        self._repository = repository
        self._exception_id_factory = exception_id_factory
        self._now = now

    def create(
        self, principal: Principal, request: RemediationExceptionRequest
    ) -> RemediationException:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(request, RemediationExceptionRequest):
            raise TypeError("request must be a RemediationExceptionRequest")
        authorize(principal, Action.MANAGE_REMEDIATION_EXCEPTIONS)
        exception_id = self._exception_id_factory()
        if not isinstance(exception_id, str) or not exception_id.strip():
            raise ValueError("generated exception_id must be a non-empty string")
        approved_at = self._now()
        if (
            not isinstance(approved_at, datetime)
            or approved_at.tzinfo is None
            or approved_at.utcoffset() is None
        ):
            raise ValueError("now must return an offset-aware datetime")
        exception = RemediationException(
            exception_id=exception_id,
            customer_id=principal.customer_id,
            rule_id=request.rule_id,
            rule_version=request.rule_version,
            resource_id=request.resource_id,
            reason=request.reason,
            approved_by=principal.subject,
            approved_at=approved_at.isoformat(),
            expires_at=request.expires_at,
            ticket_reference=request.ticket_reference,
        )
        self._repository.create_exception(exception)
        return exception
