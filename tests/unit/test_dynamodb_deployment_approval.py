"""M2 A DynamoDB approval/audit transaction tests."""

import unittest
from datetime import UTC, datetime

from apps.backend.repositories import DynamoDbDeploymentApprovalRepository
from packages.contracts import DeploymentApproval
from packages.contracts.remediation import DeploymentReadiness, DeploymentReadinessStatus


class Transactions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {}


class DynamoDbDeploymentApprovalRepositoryTest(unittest.TestCase):
    def test_writes_immutable_approval_and_audit_in_one_transaction(self) -> None:
        transactions = Transactions()
        repository = DynamoDbDeploymentApprovalRepository(
            table_name="metadata",
            transaction_client=transactions,
            now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
            id_factory=lambda: "001",
        )
        repository.record_approval(
            customer_id="cust-001",
            approval=DeploymentApproval(
                deployment_id="deployment-001",
                approved_by="admin-001",
                commit_sha="commit-001",
                plan_hash="plan-001",
            ),
            readiness=DeploymentReadiness(
                deployment_id="deployment-001",
                finding_id="finding-001",
                commit_sha="commit-001",
                plan_hash="plan-001",
                status=DeploymentReadinessStatus.READY_FOR_APPROVAL,
                reason_codes=("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT",),
            ),
        )
        items = transactions.calls[0]["TransactItems"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["Put"]["Item"]["commit_sha"], "commit-001")
        self.assertEqual(items[1]["Put"]["Item"]["event_type"], "DEPLOYMENT_APPROVED")
        self.assertNotIn("artifact", items[1]["Put"]["Item"])
