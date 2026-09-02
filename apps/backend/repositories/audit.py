"""DynamoDB read-only reader for the immutable customer audit trail.

모든 writer는 `AUDIT#{occurred_at}#{event_id}` 접두어의 `AUDIT_EVENT` item을
`CUSTOMER#{customer_id}` 파티션에 쓴다(`packages/contracts/audit.py`). 이 reader는 그
파티션을 `begins_with(SK, "AUDIT#")`로 tenant-scoped 조회하고, DynamoDB 저장 표식을 벗겨
`AuditEventView`로 투영한다. write 표면은 없다 — 감사 이력은 immutable이다.
"""

import base64
import json
from collections.abc import Mapping
from typing import Protocol

from apps.backend.repositories.errors import RepositoryError, StoredDataError
from packages.contracts import AuditEventPage, AuditEventType, AuditEventView

_AUDIT_SK_PREFIX = "AUDIT#"


class DynamoQueryTable(Protocol):
    def query(self, **kwargs: object) -> Mapping[str, object]: ...


class DynamoDbAuditEventReader:
    """Read the customer's immutable audit trail, newest first, one page at a time."""

    def __init__(self, table: DynamoQueryTable) -> None:
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
        """Return one page of audit events for a customer, newest occurrence first.

        `event_type`이 주어지면 그 종류만 남긴다(서버 측 filter). cursor는 이 reader가
        발급한 것만 받아들이고, 다른 고객 파티션을 가리키면 거부한다(tenant 격리).
        """
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if event_type is not None and not isinstance(event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType or None")

        arguments: dict[str, object] = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": _customer_pk(customer_id),
                ":prefix": _AUDIT_SK_PREFIX,
            },
            # 최신 이력이 먼저 오도록 SK 내림차순으로 읽는다.
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if event_type is not None:
            arguments["FilterExpression"] = "event_type = :event_type"
            values = arguments["ExpressionAttributeValues"]
            assert isinstance(values, dict)
            values[":event_type"] = event_type.value
        start_key = _decode_cursor(cursor, customer_id)
        if start_key is not None:
            arguments["ExclusiveStartKey"] = start_key

        try:
            response = self._table.query(**arguments)
        except Exception:
            raise RepositoryError("audit event query failed") from None
        if not isinstance(response, Mapping):
            raise StoredDataError("audit event query response is invalid")
        items = response.get("Items", [])
        if not isinstance(items, list):
            raise StoredDataError("audit event query items are invalid")
        events = tuple(_view_from_item(item, customer_id) for item in items)
        next_cursor = _encode_cursor(response.get("LastEvaluatedKey"), customer_id)
        return AuditEventPage(events=events, next_cursor=next_cursor)


def _customer_pk(customer_id: str) -> str:
    return f"CUSTOMER#{customer_id}"


def _view_from_item(item: object, customer_id: str) -> AuditEventView:
    if not isinstance(item, Mapping):
        raise StoredDataError("stored audit event item is invalid")
    if item.get("entity_type") != "AUDIT_EVENT":
        raise StoredDataError("stored audit event item is not an audit event")
    stored_customer = item.get("customer_id")
    if stored_customer != customer_id:
        # 파티션은 이미 tenant-scoped지만 item scope도 재확인해 fail-closed한다.
        raise StoredDataError("stored audit event scope is invalid")
    event_id = item.get("event_id")
    occurred_at = item.get("occurred_at")
    raw_type = item.get("event_type")
    if not isinstance(event_id, str) or not isinstance(occurred_at, str):
        raise StoredDataError("stored audit event is missing required fields")
    try:
        event_type = AuditEventType(raw_type)
    except ValueError:
        raise StoredDataError("stored audit event type is unknown") from None
    attributes = {
        key: value
        for key, value in item.items()
        if key not in {"event_id", "customer_id", "event_type", "occurred_at"}
    }
    try:
        return AuditEventView(
            event_id=event_id,
            customer_id=customer_id,
            event_type=event_type,
            occurred_at=occurred_at,
            attributes=attributes,
        )
    except (TypeError, ValueError):
        raise StoredDataError("stored audit event cannot be projected") from None


def _encode_cursor(value: object, customer_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise StoredDataError("audit event page cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    if (
        pk != _customer_pk(customer_id)
        or not isinstance(sk, str)
        or not sk.startswith(_AUDIT_SK_PREFIX)
    ):
        raise StoredDataError("audit event page cursor is outside scope")
    raw = json.dumps({"PK": pk, "SK": sk}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, customer_id: str) -> dict[str, str] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("cursor must be a non-empty string or None")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None
    if not isinstance(value, dict) or set(value) != {"PK", "SK"}:
        raise ValueError("cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    if (
        pk != _customer_pk(customer_id)
        or not isinstance(sk, str)
        or not sk.startswith(_AUDIT_SK_PREFIX)
    ):
        raise ValueError("cursor is outside customer scope")
    return {"PK": pk, "SK": sk}
