"""Admin 감사 이력 조회(`GET /audit-events`)의 저장소·서비스 경계 테스트 (M2 A).

고정하는 불변식:
- 조회는 고객 partition의 `AUDIT#` prefix를 최신순으로 읽는 단일 query다(scan 없음).
- 저장 bookkeeping(PK/SK/entity_type/GSI/version)은 응답 payload에 새어 나가지 않는다.
- cursor는 client가 준 값이므로 다른 고객 scope로 넘어가면 거부한다.
- 종류 필터는 정본 `event_type` 필드만 본다.
"""

import unittest

from apps.backend.api.audit_events import DEFAULT_PAGE_LIMIT, AuditEventApiService
from apps.backend.auth import AuthorizationDenied, Principal
from apps.backend.jobs.errors import RequestValidationError
from apps.backend.repositories import DynamoDbAuditEventRepository
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import AuditEventType

CUSTOMER_ID = "cust-001"


def _audit_item(
    *,
    event_id: str,
    occurred_at: str,
    event_type: AuditEventType = AuditEventType.DEPLOYMENT_REQUESTED,
    customer_id: str = CUSTOMER_ID,
    **details: object,
) -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": f"AUDIT#{occurred_at}#{event_id}",
        "entity_type": "AUDIT_EVENT",
        "customer_id": customer_id,
        "event_id": event_id,
        "occurred_at": occurred_at,
        "version": 1,
        "event_type": event_type.value,
        **details,
    }


class FakeTable:
    def __init__(self, items: list[dict[str, object]], last_key: object = None) -> None:
        self.items = items
        self.last_key = last_key
        self.calls: list[dict[str, object]] = []

    def query(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        response: dict[str, object] = {"Items": self.items}
        if self.last_key is not None:
            response["LastEvaluatedKey"] = self.last_key
        return response


def _principal(group: str = "Admin") -> Principal:
    return Principal.from_verified_claims(
        {
            "token_use": "access",
            "sub": "subject-001",
            "client_id": "client-001",
            "custom:customer_id": CUSTOMER_ID,
            "cognito:groups": [group],
        }
    )


class AuditEventRepositoryTest(unittest.TestCase):
    def test_reads_the_customer_partition_newest_first(self) -> None:
        table = FakeTable(
            [
                _audit_item(event_id="a2", occurred_at="2026-09-02T10:00:00Z"),
                _audit_item(event_id="a1", occurred_at="2026-09-01T10:00:00Z"),
            ]
        )
        page = DynamoDbAuditEventRepository(table).list_events(customer_id=CUSTOMER_ID, limit=10)
        call = table.calls[0]
        self.assertEqual(call["KeyConditionExpression"], "PK = :pk AND begins_with(SK, :prefix)")
        self.assertEqual(
            call["ExpressionAttributeValues"],
            {":pk": f"CUSTOMER#{CUSTOMER_ID}", ":prefix": "AUDIT#"},
        )
        self.assertIs(call["ScanIndexForward"], False)
        self.assertEqual([event.event_id for event in page.events], ["a2", "a1"])
        self.assertIsNone(page.next_cursor)

    def test_strips_storage_bookkeeping_from_the_payload(self) -> None:
        table = FakeTable(
            [
                _audit_item(
                    event_id="a1",
                    occurred_at="2026-09-01T10:00:00Z",
                    deployment_id="dep-1",
                    commit_sha="c" * 40,
                )
            ]
        )
        page = DynamoDbAuditEventRepository(table).list_events(customer_id=CUSTOMER_ID, limit=10)
        details = page.events[0].details
        self.assertEqual(dict(details), {"deployment_id": "dep-1", "commit_sha": "c" * 40})
        for stripped in ("PK", "SK", "entity_type", "version", "event_id", "event_type"):
            self.assertNotIn(stripped, details)

    def test_rejects_a_stored_event_from_another_customer(self) -> None:
        table = FakeTable(
            [_audit_item(event_id="a1", occurred_at="2026-09-01T10:00:00Z", customer_id="cust-002")]
        )
        with self.assertRaises(StoredDataError):
            DynamoDbAuditEventRepository(table).list_events(customer_id=CUSTOMER_ID, limit=10)

    def test_rejects_a_naive_timestamp(self) -> None:
        """정렬 근거가 없는 시각은 감사 항목으로 반환하지 않는다."""
        table = FakeTable([_audit_item(event_id="a1", occurred_at="2026-09-01T10:00:00")])
        with self.assertRaises(StoredDataError):
            DynamoDbAuditEventRepository(table).list_events(customer_id=CUSTOMER_ID, limit=10)

    def test_round_trips_a_scoped_cursor(self) -> None:
        table = FakeTable(
            [_audit_item(event_id="a2", occurred_at="2026-09-02T10:00:00Z")],
            last_key={"PK": f"CUSTOMER#{CUSTOMER_ID}", "SK": "AUDIT#2026-09-02T10:00:00Z#a2"},
        )
        repository = DynamoDbAuditEventRepository(table)
        first = repository.list_events(customer_id=CUSTOMER_ID, limit=1)
        self.assertIsNotNone(first.next_cursor)
        repository.list_events(customer_id=CUSTOMER_ID, limit=1, cursor=first.next_cursor)
        self.assertEqual(
            table.calls[1]["ExclusiveStartKey"],
            {"PK": f"CUSTOMER#{CUSTOMER_ID}", "SK": "AUDIT#2026-09-02T10:00:00Z#a2"},
        )

    def test_rejects_a_cursor_from_another_customer(self) -> None:
        table = FakeTable(
            [
                _audit_item(
                    event_id="a2", occurred_at="2026-09-02T10:00:00Z", customer_id="cust-002"
                )
            ],
            last_key={"PK": "CUSTOMER#cust-002", "SK": "AUDIT#2026-09-02T10:00:00Z#a2"},
        )
        foreign = DynamoDbAuditEventRepository(table).list_events(customer_id="cust-002", limit=1)
        with self.assertRaises(ValueError):
            DynamoDbAuditEventRepository(FakeTable([])).list_events(
                customer_id=CUSTOMER_ID, limit=1, cursor=foreign.next_cursor
            )

    def test_filters_on_the_canonical_event_type_field(self) -> None:
        table = FakeTable([_audit_item(event_id="a1", occurred_at="2026-09-01T10:00:00Z")])
        DynamoDbAuditEventRepository(table).list_events(
            customer_id=CUSTOMER_ID, limit=10, event_type=AuditEventType.DEPLOYMENT_REQUESTED
        )
        call = table.calls[0]
        self.assertEqual(call["FilterExpression"], "event_type = :event_type")
        self.assertEqual(call["ExpressionAttributeValues"][":event_type"], "DEPLOYMENT_REQUESTED")

    def test_rejects_an_out_of_range_limit(self) -> None:
        repository = DynamoDbAuditEventRepository(FakeTable([]))
        for limit in (0, 101, True, "10"):
            with self.assertRaises(ValueError):
                repository.list_events(customer_id=CUSTOMER_ID, limit=limit)


class AuditEventApiServiceTest(unittest.TestCase):
    def _service(self, table: FakeTable) -> AuditEventApiService:
        return AuditEventApiService(events=DynamoDbAuditEventRepository(table))

    def test_admin_reads_only_its_own_customer_partition(self) -> None:
        table = FakeTable([_audit_item(event_id="a1", occurred_at="2026-09-01T10:00:00Z")])
        page = self._service(table).list_events(_principal())
        self.assertEqual(len(page.events), 1)
        self.assertEqual(
            table.calls[0]["ExpressionAttributeValues"][":pk"], f"CUSTOMER#{CUSTOMER_ID}"
        )
        self.assertEqual(table.calls[0]["Limit"], DEFAULT_PAGE_LIMIT)

    def test_user_role_is_denied(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self._service(FakeTable([])).list_events(_principal("User"))

    def test_parses_a_string_limit_and_rejects_an_unknown_event_type(self) -> None:
        table = FakeTable([])
        self._service(table).list_events(_principal(), limit="5")
        self.assertEqual(table.calls[0]["Limit"], 5)
        with self.assertRaises(RequestValidationError):
            self._service(FakeTable([])).list_events(_principal(), event_type="NOT_A_KIND")

    def test_rejects_an_out_of_range_limit_as_a_request_error(self) -> None:
        for limit in ("0", 101, "abc"):
            with self.assertRaises(RequestValidationError):
                self._service(FakeTable([])).list_events(_principal(), limit=limit)


if __name__ == "__main__":
    unittest.main()
