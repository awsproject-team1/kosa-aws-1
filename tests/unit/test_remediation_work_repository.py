"""Authoritative DynamoDB remediation work reader tests."""

import unittest

from apps.backend.repositories import DynamoDbRemediationWorkRepository, StoredDataError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
)


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


def decision() -> RemediationDecision:
    finding = context().finding
    return RemediationDecision(
        finding_id=finding.finding_id,
        resource_id=finding.resource_id,
        rule_id=finding.rule_id,
        rule_version=finding.rule_version,
        perspective=finding.perspective,
        action=RemediationAction.TERRAFORM_PATCH,
    )


class Table:
    def __init__(self):
        self.job = {
            "job_id": "job-001",
            "job_type": "REMEDIATION",
            "customer_id": "cust-001",
            "remediation_id": "rem-001",
            "revision": 2,
        }
        self.remediation = {
            "customer_id": "cust-001",
            "remediation_id": "rem-001",
            "job_id": "job-001",
            "context": context().to_dict(),
            "decision": decision().to_dict(),
        }
        self.get_calls = []

    def query(self, **kwargs):
        return {"Items": [self.job]}

    def get_item(self, **kwargs):
        self.get_calls.append(kwargs)
        return {"Item": self.remediation}


class RemediationWorkRepositoryTest(unittest.TestCase):
    def test_reloads_context_and_decision_for_exact_revision(self):
        table = Table()

        work = DynamoDbRemediationWorkRepository(table).get_work(
            job_id="job-001", expected_revision=2
        )

        self.assertEqual(work.customer_id, "cust-001")
        self.assertEqual(work.context, context())
        self.assertEqual(work.decision, decision())
        self.assertTrue(table.get_calls[0]["ConsistentRead"])

    def test_stale_revision_does_not_read_remediation(self):
        table = Table()

        work = DynamoDbRemediationWorkRepository(table).get_work(
            job_id="job-001", expected_revision=1
        )

        self.assertIsNone(work)
        self.assertEqual(table.get_calls, [])

    def test_stored_customer_mismatch_fails_closed(self):
        table = Table()
        table.remediation["customer_id"] = "cust-002"

        with self.assertRaises(StoredDataError):
            DynamoDbRemediationWorkRepository(table).get_work(job_id="job-001", expected_revision=2)


if __name__ == "__main__":
    unittest.main()
