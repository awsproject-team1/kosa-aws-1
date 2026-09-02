"""DynamoDbAuditEventReader reads the immutable audit trail tenant-scoped."""

import base64
import json
import unittest

from apps.backend.repositories.audit import DynamoDbAuditEventReader
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import AuditEventType

CUSTOMER = "cust-001"


def _audit_item(event_id: str, occurred_at: str, event_type: str, **extra: object) -> dict:
    item = {
        "PK": f"CUSTOMER#{CUSTOMER}",
        "SK": f"AUDIT#{occurred_at}#{event_id}",
        "entity_type": "AUDIT_EVENT",
        "customer_id": CUSTOMER,
        "event_id": event_id,
        "occurred_at": occurred_at,
        "version": 1,
        "event_type": event_type,
    }
    item.update(extra)
    return item


class QueryTable:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def query(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return self.response


class FailingTable:
    def query(self, **kwargs: object) -> dict:
        raise RuntimeError("boom")


class DynamoDbAuditEventReaderTest(unittest.TestCase):
    def test_projects_items_to_views_newest_first(self) -> None:
        table = QueryTable(
            {
                "Items": [
                    _audit_item(
                        "audit-002",
                        "2026-09-03T01:00:00Z",
                        "DEPLOYMENT_APPROVED",
                        deployment_id="dep-001",
                        plan_hash="plan-001",
                    ),
                    _audit_item(
                        "audit-001",
                        "2026-09-03T00:00:00Z",
                        "DEPLOYMENT_REQUESTED",
                        deployment_id="dep-001",
                    ),
                ]
            }
        )
        page = DynamoDbAuditEventReader(table).list_events(customer_id=CUSTOMER, limit=10)
        self.assertEqual(len(page.events), 2)
        self.assertIs(page.events[0].event_type, AuditEventType.DEPLOYMENT_APPROVED)
        self.assertEqual(page.events[0].to_dict()["plan_hash"], "plan-001")
        self.assertIsNone(page.next_cursor)
        # 최신순 조회를 위해 SK 내림차순으로 읽는다.
        self.assertIs(table.calls[0]["ScanIndexForward"], False)

    def test_query_is_scoped_to_the_customer_partition(self) -> None:
        table = QueryTable({"Items": []})
        DynamoDbAuditEventReader(table).list_events(customer_id=CUSTOMER, limit=5)
        values = table.calls[0]["ExpressionAttributeValues"]
        self.assertEqual(values[":pk"], f"CUSTOMER#{CUSTOMER}")
        self.assertEqual(values[":prefix"], "AUDIT#")

    def test_event_type_filter_is_applied_server_side(self) -> None:
        table = QueryTable({"Items": []})
        DynamoDbAuditEventReader(table).list_events(
            customer_id=CUSTOMER, limit=5, event_type=AuditEventType.DEPLOYMENT_REJECTED
        )
        self.assertIn("FilterExpression", table.calls[0])
        self.assertEqual(
            table.calls[0]["ExpressionAttributeValues"][":event_type"], "DEPLOYMENT_REJECTED"
        )

    def test_next_cursor_is_returned_when_a_page_remains(self) -> None:
        table = QueryTable(
            {
                "Items": [_audit_item("audit-001", "2026-09-03T00:00:00Z", "DEPLOYMENT_REQUESTED")],
                "LastEvaluatedKey": {
                    "PK": f"CUSTOMER#{CUSTOMER}",
                    "SK": "AUDIT#2026-09-03T00:00:00Z#audit-001",
                },
            }
        )
        page = DynamoDbAuditEventReader(table).list_events(customer_id=CUSTOMER, limit=1)
        self.assertIsNotNone(page.next_cursor)

    def test_cursor_round_trips_into_exclusive_start_key(self) -> None:
        key = {"PK": f"CUSTOMER#{CUSTOMER}", "SK": "AUDIT#2026-09-03T00:00:00Z#audit-001"}
        cursor = base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")
        table = QueryTable({"Items": []})
        DynamoDbAuditEventReader(table).list_events(customer_id=CUSTOMER, limit=5, cursor=cursor)
        self.assertEqual(table.calls[0]["ExclusiveStartKey"], key)

    def test_cursor_from_another_customer_is_rejected(self) -> None:
        key = {"PK": "CUSTOMER#other", "SK": "AUDIT#2026-09-03T00:00:00Z#audit-001"}
        cursor = base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")
        table = QueryTable({"Items": []})
        with self.assertRaises(ValueError):
            DynamoDbAuditEventReader(table).list_events(
                customer_id=CUSTOMER, limit=5, cursor=cursor
            )

    def test_item_from_another_customer_fails_closed(self) -> None:
        item = _audit_item("audit-001", "2026-09-03T00:00:00Z", "DEPLOYMENT_REQUESTED")
        item["customer_id"] = "other"
        table = QueryTable({"Items": [item]})
        with self.assertRaises(StoredDataError):
            DynamoDbAuditEventReader(table).list_events(customer_id=CUSTOMER, limit=5)

    def test_unknown_event_type_fails_closed(self) -> None:
        table = QueryTable(
            {"Items": [_audit_item("audit-001", "2026-09-03T00:00:00Z", "MYSTERY_EVENT")]}
        )
        with self.assertRaises(StoredDataError):
            DynamoDbAuditEventReader(table).list_events(customer_id=CUSTOMER, limit=5)

    def test_provider_failure_is_wrapped(self) -> None:
        with self.assertRaises(RepositoryError):
            DynamoDbAuditEventReader(FailingTable()).list_events(customer_id=CUSTOMER, limit=5)

    def test_invalid_limit_and_customer_are_rejected(self) -> None:
        reader = DynamoDbAuditEventReader(QueryTable({"Items": []}))
        with self.assertRaises(ValueError):
            reader.list_events(customer_id="  ", limit=5)
        with self.assertRaises(ValueError):
            reader.list_events(customer_id=CUSTOMER, limit=0)
        with self.assertRaises(ValueError):
            reader.list_events(customer_id=CUSTOMER, limit=True)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
