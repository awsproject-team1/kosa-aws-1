"""A-owned remediation exception registration tests."""

import unittest
from datetime import UTC, datetime

from apps.backend.api.remediation_exceptions import (
    RemediationExceptionApiService,
    RemediationExceptionRequest,
)
from apps.backend.auth import AuthorizationDenied, Principal, Role
from packages.contracts import RemediationExceptionReason

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


class Repository:
    def __init__(self):
        self.values = []

    def create_exception(self, exception):
        self.values.append(exception)


def principal(role: Role, *, customer_id: str = "cust-001") -> Principal:
    return Principal(
        subject="subject-001",
        client_id="client-001",
        customer_id=customer_id,
        roles=frozenset({role}),
    )


def request() -> RemediationExceptionRequest:
    return RemediationExceptionRequest(
        rule_id="rule-001",
        rule_version="v1",
        resource_id="bucket-001",
        reason=RemediationExceptionReason.COMPENSATING_CONTROL,
        expires_at="2026-09-02T08:00:00+00:00",
        ticket_reference="TICKET-001",
    )


class RemediationExceptionApiServiceTest(unittest.TestCase):
    def test_admin_registers_server_owned_customer_approval_and_id(self):
        repository = Repository()
        service = RemediationExceptionApiService(
            repository=repository,
            exception_id_factory=lambda: "exception-001",
            now=lambda: NOW,
        )

        result = service.create(principal(Role.ADMIN), request())

        self.assertEqual(result.customer_id, "cust-001")
        self.assertEqual(result.exception_id, "exception-001")
        self.assertEqual(result.approved_by, "subject-001")
        self.assertEqual(result.approved_at, NOW.isoformat())
        self.assertEqual(repository.values, [result])

    def test_user_cannot_register_an_exception(self):
        repository = Repository()
        service = RemediationExceptionApiService(
            repository=repository,
            exception_id_factory=lambda: "exception-001",
            now=lambda: NOW,
        )

        with self.assertRaises(AuthorizationDenied):
            service.create(principal(Role.USER), request())

        self.assertEqual(repository.values, [])

    def test_expiry_must_be_after_server_approval_time(self):
        service = RemediationExceptionApiService(
            repository=Repository(),
            exception_id_factory=lambda: "exception-001",
            now=lambda: NOW,
        )
        invalid = RemediationExceptionRequest(
            rule_id="rule-001",
            rule_version="v1",
            reason=RemediationExceptionReason.ACCEPTED_RISK,
            expires_at="2026-08-31T08:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "later"):
            service.create(principal(Role.ADMIN), invalid)


if __name__ == "__main__":
    unittest.main()
