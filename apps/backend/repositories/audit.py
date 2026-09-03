"""A-owned tenant-scoped reader for the immutable audit trail (M2 A).

감사 event는 일곱 writer가 `AUDIT#{occurred_at}#{event_id}` 하나의 SK 규약으로 쓰고, 종류는
모두 `event_type` 한 필드에 담는다(DATABASE.md). 그래서 조회는 writer별 분기 없이 고객
partition의 `AUDIT#` prefix를 최신순으로 훑는 단일 query다. scan은 쓰지 않는다.
"""

import base64
import json
from collections.abc import Mapping

from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import AuditEvent, AuditEventPage, AuditEventType, audit_event_details

_SK_PREFIX = "AUDIT#"


class DynamoDbAuditEventRepository:
    """Read one customer's audit trail newest-first, page by page."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def list_events(
        self,
        *,
        customer_id: str,
        limit: int,
        cursor: str | None = None,
        event_type: AuditEventType | None = None,
    ) -> AuditEventPage:
        """Return one page of audit events, newest first.

        `event_type` filters after the key condition. DynamoDB applies a filter to the
        already-read page, so a filtered page can be shorter than `limit` — even empty —
        while `next_cursor` is still set. Callers must follow the cursor rather than
        treating a short page as the end of the trail; padding the page here would need
        an unbounded read loop for a rare event kind.
        """
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100")
        if event_type is not None and not isinstance(event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType or None")
        start_key = _decode_cursor(cursor, customer_id)
        arguments: dict[str, object] = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": f"CUSTOMER#{customer_id}",
                ":prefix": _SK_PREFIX,
            },
            # Newest first: the trail is read for "what just happened", and the SK's
            # leading timestamp makes descending order a key-order read, not a sort.
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if start_key is not None:
            arguments["ExclusiveStartKey"] = start_key
        if event_type is not None:
            arguments["FilterExpression"] = "event_type = :event_type"
            values = arguments["ExpressionAttributeValues"]
            assert isinstance(values, dict)
            values[":event_type"] = event_type.value
        try:
            response = self._table.query(**arguments)
        except Exception:
            raise RepositoryError("audit event read failed") from None
        if not isinstance(response, Mapping):
            raise StoredDataError("audit event page is invalid")
        items = response.get("Items", [])
        if not isinstance(items, list):
            raise StoredDataError("audit event page is invalid")
        events = tuple(_event_from_item(item, customer_id) for item in items)
        return AuditEventPage(
            events=events,
            next_cursor=_encode_cursor(response.get("LastEvaluatedKey"), customer_id),
        )


def _event_from_item(item: object, customer_id: str) -> AuditEvent:
    if not isinstance(item, Mapping):
        raise StoredDataError("stored audit event is invalid")
    # The query is key-scoped to this customer, but a stored item that disagrees with
    # its own key is corrupt, not merely unexpected: returning it would publish another
    # tenant's payload under this caller's scope.
    if item.get("customer_id") != customer_id or item.get("entity_type") != "AUDIT_EVENT":
        raise StoredDataError("stored audit event is outside the customer scope")
    try:
        return AuditEvent(
            event_id=_string(item.get("event_id"), "event_id"),
            event_type=AuditEventType(item.get("event_type")),
            occurred_at=_string(item.get("occurred_at"), "occurred_at"),
            customer_id=customer_id,
            details=audit_event_details(item),
        )
    except StoredDataError:
        raise
    except (TypeError, ValueError):
        raise StoredDataError("stored audit event is invalid") from None


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoredDataError(f"audit event {name} is invalid")
    return value


def _encode_cursor(value: object, customer_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise StoredDataError("audit event page cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    if pk != f"CUSTOMER#{customer_id}" or not isinstance(sk, str) or not sk.startswith(_SK_PREFIX):
        raise StoredDataError("audit event page cursor is outside scope")
    raw = json.dumps({"PK": pk, "SK": sk}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, customer_id: str) -> dict[str, str] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip():
        raise ValueError("cursor must be a non-empty string or None")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("cursor is invalid") from None
    if not isinstance(value, dict) or set(value) != {"PK", "SK"}:
        raise ValueError("cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    # A cursor is client-supplied. Without this check a caller could hand back another
    # customer's key and page through their trail under their own token.
    if pk != f"CUSTOMER#{customer_id}" or not isinstance(sk, str) or not sk.startswith(_SK_PREFIX):
        raise ValueError("cursor is outside the customer scope")
    return {"PK": pk, "SK": sk}
