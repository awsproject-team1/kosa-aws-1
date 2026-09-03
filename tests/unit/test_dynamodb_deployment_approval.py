"""M2 A DynamoDB approval/audit transaction tests."""

import unittest
from collections.abc import Mapping
from datetime import UTC, datetime

from apps.backend.repositories import DynamoDbDeploymentApprovalRepository
from apps.backend.repositories.errors import RepositoryError
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import DeploymentApproval
from packages.contracts.remediation import DeploymentReadiness, DeploymentReadinessStatus


class Transactions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {}


class FakeReadTable:
    """A resource table stub keyed by (PK, SK) with auto-unmarshalled items."""

    def __init__(self, items: dict[tuple[str, str], Mapping[str, object]]) -> None:
        self._items = items

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        stored = self._items.get((key["PK"], key["SK"]))
        return {} if stored is None else {"Item": stored}


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

    def test_get_approval_reconstructs_the_stored_approval(self) -> None:
        # record_approval이 쓴 것과 같은 key/body를 read table에 넣어 왕복을 확인한다.
        approval_item = {
            "PK": "CUSTOMER#cust-001",
            "SK": "DEPLOYMENT#deployment-001#APPROVAL#approval-deployment-001",
            "entity_type": "DEPLOYMENT_APPROVAL",
            "deployment_id": "deployment-001",
            "approved_by": "admin-001",
            "commit_sha": "commit-001",
            "plan_hash": "plan-001",
        }
        repository = DynamoDbDeploymentApprovalRepository(
            table_name="metadata",
            transaction_client=Transactions(),
            table=FakeReadTable({("CUSTOMER#cust-001", approval_item["SK"]): approval_item}),
        )
        approval = repository.get_approval(customer_id="cust-001", deployment_id="deployment-001")
        self.assertEqual(
            approval,
            DeploymentApproval(
                deployment_id="deployment-001",
                approved_by="admin-001",
                commit_sha="commit-001",
                plan_hash="plan-001",
            ),
        )

    def test_get_approval_returns_none_when_absent(self) -> None:
        repository = DynamoDbDeploymentApprovalRepository(
            table_name="metadata",
            transaction_client=Transactions(),
            table=FakeReadTable({}),
        )
        self.assertIsNone(
            repository.get_approval(customer_id="cust-001", deployment_id="deployment-404")
        )

    def test_get_approval_fails_closed_without_a_read_table(self) -> None:
        repository = DynamoDbDeploymentApprovalRepository(
            table_name="metadata", transaction_client=Transactions()
        )
        with self.assertRaises(RepositoryError):
            repository.get_approval(customer_id="cust-001", deployment_id="deployment-001")

    def test_get_approval_rejects_a_wrong_entity_type(self) -> None:
        wrong = {
            "PK": "CUSTOMER#cust-001",
            "SK": "DEPLOYMENT#deployment-001#APPROVAL#approval-deployment-001",
            "entity_type": "AUDIT_EVENT",
        }
        repository = DynamoDbDeploymentApprovalRepository(
            table_name="metadata",
            transaction_client=Transactions(),
            table=FakeReadTable({("CUSTOMER#cust-001", wrong["SK"]): wrong}),
        )
        with self.assertRaises(StoredDataError):
            repository.get_approval(customer_id="cust-001", deployment_id="deployment-001")
