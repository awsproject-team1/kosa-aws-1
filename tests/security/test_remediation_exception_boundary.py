"""Security tests for A-owned remediation exception boundaries."""

import unittest
from datetime import UTC, datetime

from apps.backend.api.remediation_exceptions import (
    RemediationExceptionApiService,
    RemediationExceptionRequest,
)
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.repositories import (
    DynamoDbRemediationExceptionRepository,
    StoredDataError,
)
from packages.contracts import (
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    RemediationException,
    RemediationExceptionReason,
)


class Sink:
    def __init__(self):
        self.values = []

    def create_exception(self, exception):
        self.values.append(exception)


class Table:
    def __init__(self, items):
        self.items = items

    def query(self, **kwargs):
        return {"Items": self.items}


class Transactions:
    def transact_write_items(self, **kwargs):
        return None


def principal(role: Role, customer_id="cust-001") -> Principal:
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
        reason=RemediationExceptionReason.ACCEPTED_RISK,
        expires_at="2026-09-02T08:00:00+00:00",
    )


def finding() -> Finding:
    return Finding(
        finding_id="finding-001",
        resource_id="bucket-001",
        rule_id="rule-001",
        rule_version="v1",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=0,
        rationale="unsafe",
        evidence_references=("terraform:bucket-001",),
    )


class RemediationExceptionBoundarySecurityTest(unittest.TestCase):
    def test_user_cannot_approve_customer_exception(self):
        sink = Sink()
        service = RemediationExceptionApiService(
            repository=sink,
            exception_id_factory=lambda: "exception-001",
            now=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )

        with self.assertRaises(AuthorizationDenied):
            service.create(principal(Role.USER), request())
        self.assertEqual(sink.values, [])

    def test_reason_is_enum_only_and_request_has_no_tenant_or_approval_fields(self):
        with self.assertRaisesRegex(TypeError, "reason"):
            RemediationExceptionRequest(
                rule_id="rule-001",
                rule_version="v1",
                reason="because the policy text says so",
                expires_at="2026-09-02T08:00:00+00:00",
            )
        self.assertEqual(
            set(RemediationExceptionRequest.__dataclass_fields__),
            {
                "rule_id",
                "rule_version",
                "reason",
                "expires_at",
                "resource_id",
                "ticket_reference",
            },
        )

    def test_cross_tenant_exception_item_is_not_accepted_as_policy_input(self):
        other = RemediationException(
            exception_id="exception-001",
            customer_id="cust-002",
            rule_id="rule-001",
            rule_version="v1",
            reason=RemediationExceptionReason.ACCEPTED_RISK,
            approved_by="admin-002",
            approved_at="2026-09-01T08:00:00+00:00",
            expires_at="2026-09-02T08:00:00+00:00",
        )
        repository = DynamoDbRemediationExceptionRepository(
            Table([other.to_dict()]),
            table_name="metadata",
            transaction_client=Transactions(),
        )

        with self.assertRaises(StoredDataError):
            repository.list_exceptions(customer_id="cust-001", finding=finding())


if __name__ == "__main__":
    unittest.main()
