"""M3 A GET /audit-events: Admin-only read of the immutable audit trail."""

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from packages.contracts import AuditEventPage, AuditEventType

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


class AuditEventReader(Protocol):
    """Read one tenant-scoped page of the customer's audit trail."""

    def list_events(
        self,
        *,
        customer_id: str,
        limit: int,
        cursor: str | None = None,
        event_type: AuditEventType | None = None,
    ) -> AuditEventPage: ...


class AuditEventApiService:
    """Authorize an Admin principal and return their customer's audit trail.

    감사 이력은 관리자만 조회한다(`READ_AUDIT_EVENTS`, Admin 전용). 조회는 항상
    principal의 customer scope 안에서만 이뤄지므로 client는 다른 고객 이력을 볼 수 없다.
    """

    def __init__(self, reader: AuditEventReader) -> None:
        if reader is None:
            raise TypeError("reader is required")
        self._reader = reader

    def list_events(
        self,
        principal: Principal,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        event_type: AuditEventType | None = None,
    ) -> AuditEventPage:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        authorize(principal, Action.READ_AUDIT_EVENTS)
        page_limit = _DEFAULT_LIMIT if limit is None else limit
        if isinstance(page_limit, bool) or not isinstance(page_limit, int) or page_limit <= 0:
            raise ValueError("limit must be a positive integer")
        if page_limit > _MAX_LIMIT:
            raise ValueError("limit exceeds the maximum page size")
        if event_type is not None and not isinstance(event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType or None")
        return self._reader.list_events(
            customer_id=principal.customer_id,
            limit=page_limit,
            cursor=cursor,
            event_type=event_type,
        )
