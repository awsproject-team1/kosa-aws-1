"""DynamoDB remediation workflow persistence tests."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from apps.backend.jobs import WorkflowOutboxEntry, create_job
from apps.backend.repositories import DynamoDbAssessmentWorkflowRepository
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    JobCurrentStep,
    ManualReviewCode,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    WorkflowCommand,
    WorkflowTask,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _unmarshal(value):
    """Convert a low-level DynamoDB AttributeValue tree back to plain Python.

    Transaction writes go through the low-level client, so items are marshaled as
    AttributeValues ({"S": ...}, {"M": {...}}, {"N": ...}); unwrap them for assertions.
    """
    if isinstance(value, dict) and len(value) == 1:
        ((tag, inner),) = value.items()
        if tag == "S":
            return inner
        if tag == "N":
            return int(inner) if str(inner).lstrip("-").isdigit() else float(inner)
        if tag == "BOOL":
            return inner
        if tag == "NULL":
            return None
        if tag == "M":
            return {key: _unmarshal(item) for key, item in inner.items()}
        if tag == "L":
            return [_unmarshal(item) for item in inner]
    if isinstance(value, dict):
        return {key: _unmarshal(item) for key, item in value.items()}
    return value


def _item(put) -> dict:
    return _unmarshal(put["Put"]["Item"])


class Table:
    def query(self, **kwargs):
        return {"Items": []}

    def update_item(self, **kwargs):
        return None

    def get_item(self, **kwargs):
        return {}

    def put_item(self, **kwargs):
        return None


class Transactions:
    def __init__(self):
        self.calls = []

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)


def context() -> RemediationContext:
    finding = Finding(
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
    return RemediationContext(
        finding=finding,
        snapshot=IaCSnapshot(
            customer_id="cust-001",
            repository_id="repo-001",
            commit_sha="commit-001",
            artifact=ArtifactReference(
                artifact_id="snapshot-001",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256="snapshot-hash",
                customer_id="cust-001",
                repository_id="repo-001",
            ),
        ),
        evidence_references=finding.evidence_references,
    )


def decision(action: RemediationAction) -> RemediationDecision:
    finding = context().finding
    return RemediationDecision(
        finding_id=finding.finding_id,
        resource_id=finding.resource_id,
        rule_id=finding.rule_id,
        rule_version=finding.rule_version,
        perspective=finding.perspective,
        action=action,
        manual_review_code=(
            ManualReviewCode.RULE_NOT_IN_SCOPE
            if action is RemediationAction.MANUAL_REVIEW
            else None
        ),
    )


class RemediationWorkflowRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.transactions = Transactions()
        self.repository = DynamoDbAssessmentWorkflowRepository(
            Table(), table_name="metadata", transaction_client=self.transactions
        )

    def test_actionable_transaction_persists_decision_context_job_outbox_and_audit(self):
        job = replace(
            create_job(
                job_id="job-001",
                customer_id="cust-001",
                job_type="REMEDIATION",
                initial_step=JobCurrentStep.GENERATE_REMEDIATION,
                requested_by="user-001",
            ),
            remediation_id="rem-001",
        )
        outbox = WorkflowOutboxEntry(
            customer_id="cust-001",
            job_id="job-001",
            task=WorkflowTask(
                job_id="job-001",
                expected_revision=0,
                command=WorkflowCommand.GENERATE_REMEDIATION,
            ),
        )

        self.repository.create_remediation_workflow(
            context=context(),
            decision=decision(RemediationAction.TERRAFORM_PATCH),
            job=job,
            remediation_id="rem-001",
            outbox=outbox,
            decided_at=NOW,
        )

        puts = self.transactions.calls[0]["TransactItems"]
        self.assertEqual(len(puts), 4)
        remediation = _item(puts[0])
        self.assertEqual(remediation["decision"]["action"], "TERRAFORM_PATCH")
        self.assertNotIn("strategy", remediation)
        self.assertNotIn("strategy", remediation["context"])
        audit = _item(puts[3])
        # The two fields mean different things in one item: the audit kind and the
        # decided RemediationAction.  Unifying them on `action` would lose one.
        self.assertEqual(audit["event_type"], "REMEDIATION_DECIDED")
        self.assertEqual(audit["action"], "TERRAFORM_PATCH")

    def test_non_actionable_transaction_has_no_job_or_outbox(self):
        self.repository.record_remediation_decision(
            context=context(),
            decision=decision(RemediationAction.MANUAL_REVIEW),
            remediation_id="rem-001",
            requested_by="user-001",
            decided_at=NOW,
        )

        puts = self.transactions.calls[0]["TransactItems"]
        self.assertEqual(len(puts), 2)
        entity_types = {_item(entry)["entity_type"] for entry in puts}
        self.assertEqual(entity_types, {"REMEDIATION", "AUDIT_EVENT"})
        self.assertEqual(_item(puts[0])["status"], "DECIDED_NO_ACTION")

    def test_command_must_match_stored_decision(self):
        job = replace(
            create_job(
                job_id="job-001",
                customer_id="cust-001",
                job_type="REMEDIATION",
                initial_step=JobCurrentStep.SYNC_ACTUAL_STATE,
                requested_by="user-001",
            ),
            remediation_id="rem-001",
        )
        outbox = WorkflowOutboxEntry(
            customer_id="cust-001",
            job_id="job-001",
            task=WorkflowTask(
                job_id="job-001",
                expected_revision=0,
                command=WorkflowCommand.GENERATE_REMEDIATION,
            ),
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.repository.create_remediation_workflow(
                context=context(),
                decision=decision(RemediationAction.ACTUAL_SYNC),
                job=job,
                remediation_id="rem-001",
                outbox=outbox,
                decided_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
