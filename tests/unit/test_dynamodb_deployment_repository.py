"""DynamoDbDeploymentRepository writes and reads a deployment record (ADR-0019 §4)."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from apps.backend.deployment import DeploymentRecord
from apps.backend.jobs.lifecycle import create_job
from apps.backend.jobs.outbox import WorkflowOutboxEntry
from apps.backend.repositories.deployment import DynamoDbDeploymentRepository
from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.ports import DuplicateJobError, StoredDataError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    JobCurrentStep,
    TerraformStateVersion,
    WorkflowCommand,
    WorkflowTask,
)

CUSTOMER = "cust-001"
REPO = "repo-001"
DEPLOYMENT = "deployment-001"
JOB = "job-001"


class Transactions:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._error = error

    def transact_write_items(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {}


class ReadTable:
    def __init__(self, item: dict[str, object] | None) -> None:
        self._item = item
        self.keys: list[object] = []

    def get_item(self, **kwargs: object) -> dict[str, object]:
        self.keys.append(kwargs.get("Key"))
        return {} if self._item is None else {"Item": self._item}


class ConditionalError(Exception):
    def __init__(self) -> None:
        super().__init__("conflict")
        self.response = {"Error": {"Code": "TransactionCanceledException"}}


def _record(**overrides: object) -> DeploymentRecord:
    base: dict[str, object] = {
        "deployment_id": DEPLOYMENT,
        "customer_id": CUSTOMER,
        "repository_id": REPO,
        "job_id": JOB,
        "remediation_id": "remediation-001",
        "commit_sha": "commit-001",
        "plan_hash": "plan-001",
        "plan_artifact": ArtifactReference(
            artifact_id="art-plan-001",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256="plan-001",
            customer_id=CUSTOMER,
            repository_id=REPO,
        ),
        "binary_artifact": ArtifactReference(
            artifact_id="art-plan-binary-001",
            artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
            content_sha256="binary-001",
            customer_id=CUSTOMER,
            repository_id=REPO,
        ),
        "state_version": TerraformStateVersion(lineage="lineage-1", serial=3),
        "source_assessment_id": "asm-001",
    }
    base.update(overrides)
    return DeploymentRecord(**base)  # type: ignore[arg-type]


def _job_and_outbox() -> tuple[object, WorkflowOutboxEntry]:
    job = create_job(
        job_id=JOB,
        customer_id=CUSTOMER,
        job_type="DEPLOYMENT",
        initial_step=JobCurrentStep.TERRAFORM_PLAN,
        requested_by="user-001",
    )
    job = replace(job, deployment_id=DEPLOYMENT)
    outbox = WorkflowOutboxEntry(
        customer_id=CUSTOMER,
        job_id=JOB,
        task=WorkflowTask(job_id=JOB, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT),
    )
    return job, outbox


class DynamoDbDeploymentRepositoryTest(unittest.TestCase):
    def test_creates_four_items_in_one_transaction(self) -> None:
        transactions = Transactions()
        repository = DynamoDbDeploymentRepository(
            table=ReadTable(None),
            table_name="metadata",
            transaction_client=transactions,
            now=lambda: datetime(2026, 9, 1, tzinfo=UTC),
            id_factory=lambda: "001",
        )
        job, outbox = _job_and_outbox()
        repository.create_deployment(_record(), job=job, outbox=outbox)
        items = transactions.calls[0]["TransactItems"]
        self.assertEqual(len(items), 4)
        entity_types = [item["Put"]["Item"]["entity_type"]["S"] for item in items]
        self.assertEqual(entity_types, ["DEPLOYMENT", "JOB", "WORKFLOW_OUTBOX", "AUDIT_EVENT"])
        self.assertEqual(items[3]["Put"]["Item"]["event_type"]["S"], "DEPLOYMENT_REQUESTED")
        self.assertEqual(items[2]["Put"]["Item"]["command"]["S"], "RUN_DEPLOYMENT")

    def test_conflict_maps_to_duplicate(self) -> None:
        repository = DynamoDbDeploymentRepository(
            table=ReadTable(None),
            table_name="metadata",
            transaction_client=Transactions(ConditionalError()),
        )
        job, outbox = _job_and_outbox()
        with self.assertRaises(DuplicateJobError):
            repository.create_deployment(_record(), job=job, outbox=outbox)

    def test_rejects_non_run_deployment_command(self) -> None:
        repository = DynamoDbDeploymentRepository(
            table=ReadTable(None), table_name="metadata", transaction_client=Transactions()
        )
        job, _ = _job_and_outbox()
        bad_outbox = WorkflowOutboxEntry(
            customer_id=CUSTOMER,
            job_id=JOB,
            task=WorkflowTask(
                job_id=JOB, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
            ),
        )
        bad_outbox = replace(
            bad_outbox,
            task=WorkflowTask(
                job_id=JOB, expected_revision=0, command=WorkflowCommand.ASSESS_RESOURCE
            ),
        )
        with self.assertRaisesRegex(ValueError, "RUN_DEPLOYMENT"):
            repository.create_deployment(_record(), job=job, outbox=bad_outbox)

    def test_reads_back_a_stored_record(self) -> None:
        stored = marshal_item(
            {
                "PK": f"CUSTOMER#{CUSTOMER}",
                "SK": f"DEPLOYMENT#{DEPLOYMENT}",
                "entity_type": "DEPLOYMENT",
                **_record().to_dict(),
            }
        )
        # Unmarshalled resource-table read returns plain Python values.
        plain = {
            "entity_type": "DEPLOYMENT",
            **_record().to_dict(),
        }
        repository = DynamoDbDeploymentRepository(
            table=ReadTable(plain), table_name="metadata", transaction_client=Transactions()
        )
        record = repository.get_deployment(customer_id=CUSTOMER, deployment_id=DEPLOYMENT)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.plan_hash, "plan-001")
        self.assertEqual(record.state_version, TerraformStateVersion(lineage="lineage-1", serial=3))
        self.assertIsNone(stored.get("nonexistent"))

    def test_missing_record_returns_none(self) -> None:
        repository = DynamoDbDeploymentRepository(
            table=ReadTable(None), table_name="metadata", transaction_client=Transactions()
        )
        self.assertIsNone(repository.get_deployment(customer_id=CUSTOMER, deployment_id=DEPLOYMENT))

    def test_wrong_customer_scope_is_rejected(self) -> None:
        plain = {"entity_type": "DEPLOYMENT", **_record().to_dict()}
        repository = DynamoDbDeploymentRepository(
            table=ReadTable(plain), table_name="metadata", transaction_client=Transactions()
        )
        with self.assertRaises(StoredDataError):
            repository.get_deployment(customer_id="other", deployment_id=DEPLOYMENT)


if __name__ == "__main__":
    unittest.main()
