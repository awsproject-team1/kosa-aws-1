"""M3 A GET /audit-events is Admin-only and scoped to the caller's customer."""

import unittest

from apps.backend.api.audit import AuditEventApiService
from apps.backend.auth import AuthorizationDenied, Principal, Role
from packages.contracts import AuditEventPage, AuditEventType, AuditEventView

CUSTOMER = "cust-001"


class Reader:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def list_events(self, *, customer_id, limit, cursor=None, event_type=None) -> AuditEventPage:
        self.calls.append(
            {
                "customer_id": customer_id,
                "limit": limit,
                "cursor": cursor,
                "event_type": event_type,
            }
        )
        return AuditEventPage(
            events=(
                AuditEventView(
                    event_id="audit-001",
                    customer_id=customer_id,
                    event_type=AuditEventType.DEPLOYMENT_REQUESTED,
                    occurred_at="2026-09-03T00:00:00Z",
                    attributes={"deployment_id": "dep-001"},
                ),
            )
        )


def _principal(role: Role) -> Principal:
    return Principal(
        subject="subject-001",
        client_id="client-001",
        customer_id=CUSTOMER,
        roles=frozenset({role}),
    )


class AuditEventApiServiceTest(unittest.TestCase):
    def test_admin_reads_the_audit_trail_in_their_own_customer_scope(self) -> None:
        reader = Reader()
        page = AuditEventApiService(reader).list_events(_principal(Role.ADMIN))
        self.assertEqual(len(page.events), 1)
        self.assertEqual(reader.calls[0]["customer_id"], CUSTOMER)

    def test_non_admin_is_denied(self) -> None:
        reader = Reader()
        with self.assertRaises(AuthorizationDenied):
            AuditEventApiService(reader).list_events(_principal(Role.USER))
        self.assertEqual(reader.calls, [])

    def test_default_and_custom_limit_are_forwarded(self) -> None:
        reader = Reader()
        service = AuditEventApiService(reader)
        service.list_events(_principal(Role.ADMIN))
        self.assertEqual(reader.calls[0]["limit"], 50)
        service.list_events(_principal(Role.ADMIN), limit=10)
        self.assertEqual(reader.calls[1]["limit"], 10)

    def test_event_type_and_cursor_are_forwarded(self) -> None:
        reader = Reader()
        AuditEventApiService(reader).list_events(
            _principal(Role.ADMIN),
            cursor="cursor-abc",
            event_type=AuditEventType.DEPLOYMENT_APPROVED,
        )
        self.assertEqual(reader.calls[0]["cursor"], "cursor-abc")
        self.assertIs(reader.calls[0]["event_type"], AuditEventType.DEPLOYMENT_APPROVED)

    def test_invalid_limits_are_rejected(self) -> None:
        service = AuditEventApiService(Reader())
        for bad in (0, -1, 101, True):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    service.list_events(_principal(Role.ADMIN), limit=bad)  # type: ignore[arg-type]

    def test_non_principal_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            AuditEventApiService(Reader()).list_events({"role": "Admin"})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
