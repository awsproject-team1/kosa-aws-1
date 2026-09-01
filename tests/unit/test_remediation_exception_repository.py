"""DynamoDB remediation exception repository tests."""

import unittest

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


class Table:
    def __init__(self, items=None):
        self.items = [] if items is None else items
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {"Items": self.items}


class Transactions:
    def __init__(self):
        self.calls = []

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)


def exception(*, customer_id="cust-001", resource_id="bucket-001"):
    return RemediationException(
        exception_id="exception-001",
        customer_id=customer_id,
        rule_id="rule-001",
        rule_version="v1",
        resource_id=resource_id,
        reason=RemediationExceptionReason.ACCEPTED_RISK,
        approved_by="admin-001",
        approved_at="2026-09-01T08:00:00+00:00",
        expires_at="2026-09-02T08:00:00+00:00",
        ticket_reference="TICKET-001",
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


class RemediationExceptionRepositoryTest(unittest.TestCase):
    def test_create_is_immutable_exception_and_audit_transaction(self):
        transactions = Transactions()
        repository = DynamoDbRemediationExceptionRepository(
            Table(), table_name="metadata", transaction_client=transactions
        )

        repository.create_exception(exception())

        puts = transactions.calls[0]["TransactItems"]
        self.assertEqual(len(puts), 2)
        exception_item = puts[0]["Put"]["Item"]
        audit_item = puts[1]["Put"]["Item"]
        self.assertEqual(exception_item["customer_id"], "cust-001")
        self.assertEqual(exception_item["expires_at"], "2026-09-02T08:00:00+00:00")
        self.assertIn("attribute_not_exists", puts[0]["Put"]["ConditionExpression"])
        self.assertEqual(audit_item["event_type"], "REMEDIATION_EXCEPTION_APPROVED")
        self.assertNotIn("ticket_reference", audit_item)

    def test_list_uses_customer_partition_and_exact_rule_version_prefix(self):
        global_exception = exception(resource_id=None).to_dict() | {"PK": "CUSTOMER#cust-001"}
        other_resource = exception(resource_id="bucket-002").to_dict()
        table = Table([global_exception, other_resource])
        repository = DynamoDbRemediationExceptionRepository(
            table, table_name="metadata", transaction_client=Transactions()
        )

        result = repository.list_exceptions(customer_id="cust-001", finding=finding())

        self.assertEqual(len(result), 1)
        values = table.calls[0]["ExpressionAttributeValues"]
        self.assertEqual(values[":pk"], "CUSTOMER#cust-001")
        self.assertIn("RULE#rule-001#VERSION#v1", values[":prefix"])

    def test_cross_tenant_stored_item_is_rejected(self):
        table = Table([exception(customer_id="cust-002").to_dict()])
        repository = DynamoDbRemediationExceptionRepository(
            table, table_name="metadata", transaction_client=Transactions()
        )

        with self.assertRaises(StoredDataError):
            repository.list_exceptions(customer_id="cust-001", finding=finding())


if __name__ == "__main__":
    unittest.main()
