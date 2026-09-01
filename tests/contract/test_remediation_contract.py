"""Contract tests for the C context and policy-gated public remediation response."""

import unittest
from dataclasses import fields

from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    JobCurrentStep,
    JobResponse,
    JobStatus,
    ManualReviewCode,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationStartResponse,
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


def decision(action: RemediationAction) -> RemediationDecision:
    value = finding()
    return RemediationDecision(
        finding_id=value.finding_id,
        resource_id=value.resource_id,
        rule_id=value.rule_id,
        rule_version=value.rule_version,
        perspective=value.perspective,
        action=action,
        manual_review_code=(
            ManualReviewCode.RULE_NOT_IN_SCOPE
            if action is RemediationAction.MANUAL_REVIEW
            else None
        ),
    )


class RemediationContractTest(unittest.TestCase):
    def test_context_has_no_duplicate_action_field(self):
        self.assertEqual(
            {field.name for field in fields(RemediationContext)},
            {"finding", "snapshot", "evidence_references"},
        )
        context = RemediationContext(
            finding=finding(),
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
            evidence_references=("terraform:bucket-001",),
        )
        self.assertNotIn("strategy", context.to_dict())

    def test_non_actionable_response_has_decision_and_null_job(self):
        response = RemediationStartResponse(decision=decision(RemediationAction.MANUAL_REVIEW))

        self.assertFalse(response.accepted)
        self.assertIsNone(response.to_dict()["job"])
        self.assertEqual(
            response.to_dict()["decision"]["manual_review_code"],
            "RULE_NOT_IN_SCOPE",
        )

    def test_actionable_response_requires_a_job(self):
        patch_decision = decision(RemediationAction.TERRAFORM_PATCH)
        with self.assertRaisesRegex(ValueError, "actionable"):
            RemediationStartResponse(decision=patch_decision)

        response = RemediationStartResponse(
            decision=patch_decision,
            job=JobResponse(
                job_id="job-001",
                job_type="REMEDIATION",
                status=JobStatus.QUEUED,
                current_step=JobCurrentStep.GENERATE_REMEDIATION,
                remediation_id="rem-001",
            ),
        )
        self.assertTrue(response.accepted)
        self.assertEqual(response.to_dict()["job"]["remediation_id"], "rem-001")


if __name__ == "__main__":
    unittest.main()
