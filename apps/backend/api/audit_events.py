"""Admin-only audit trail read boundary (`GET /audit-events`, M2 A)."""

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs.errors import RequestValidationError
from packages.contracts import AuditEventPage, AuditEventType

DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 100


class AuditEventReader(Protocol):
    def list_events(
        self,
        *,
        customer_id: str,
        limit: int,
        cursor: str | None = None,
        event_type: AuditEventType | None = None,
    ) -> AuditEventPage: ...


class AuditEventApiService:
    """Return one page of the caller's own audit trail.

    The trail is tenant-scoped by the principal, never by a request parameter: an
    audit reader that accepted a `customer_id` from the client would be the one
    endpoint that reads every tenant's history.
    """

    def __init__(self, *, events: AuditEventReader) -> None:
        if events is None:
            raise TypeError("events reader is required")
        self._events = events

    def list_events(
        self,
        principal: Principal,
        *,
        limit: object = None,
        cursor: object = None,
        event_type: object = None,
    ) -> AuditEventPage:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        authorize(principal, Action.READ_AUDIT_EVENTS)
        return self._events.list_events(
            customer_id=principal.customer_id,
            limit=_limit(limit),
            cursor=_cursor(cursor),
            event_type=_event_type(event_type),
        )


def _limit(value: object) -> int:
    if value is None:
        return DEFAULT_PAGE_LIMIT
    if isinstance(value, bool):
        raise RequestValidationError("limit must be an integer")
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            raise RequestValidationError("limit must be an integer") from None
    if not isinstance(value, int) or not 1 <= value <= MAX_PAGE_LIMIT:
        raise RequestValidationError(f"limit must be an integer from 1 through {MAX_PAGE_LIMIT}")
    return value


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError("cursor must be a non-empty string")
    return value


def _event_type(value: object) -> AuditEventType | None:
    if value is None:
        return None
    if isinstance(value, AuditEventType):
        return value
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError("event_type must be a non-empty string")
    try:
        return AuditEventType(value)
    except ValueError:
        # An unknown kind is a client error, not an empty page: silently returning
        # nothing would read as "no such events happened".
        raise RequestValidationError("event_type is not a known audit event type") from None
