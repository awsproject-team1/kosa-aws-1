"""Contract tests for the GitHub/AWS read-only and approval boundaries."""

import json
import unittest
from pathlib import Path

from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    AwsResourceOperation,
    AwsResourceQuery,
    DeploymentApproval,
    IaCSnapshot,
    RemediationPatch,
    TerraformPlan,
)

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "remediation_plan.json"


def artifact_from(data: dict[str, str | None]) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=str(data["artifact_id"]),
        artifact_type=ArtifactType(str(data["artifact_type"])),
        content_sha256=str(data["content_sha256"]),
        customer_id=str(data["customer_id"]),
        repository_id=data["repository_id"],
    )


class DeploymentContractTest(unittest.TestCase):
    def test_remediation_fixture_preserves_scope_and_plan_binding(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text())
        snapshot_data = fixture["snapshot"]
        snapshot = IaCSnapshot(
            customer_id=snapshot_data["customer_id"],
            repository_id=snapshot_data["repository_id"],
            commit_sha=snapshot_data["commit_sha"],
            artifact=artifact_from(snapshot_data["artifact"]),
        )
        patch_data = fixture["patch"]
        patch = RemediationPatch(
            finding_id=patch_data["finding_id"],
            base_commit_sha=patch_data["base_commit_sha"],
            artifact=artifact_from(patch_data["artifact"]),
            changed_paths=tuple(patch_data["changed_paths"]),
        )
        plan_data = fixture["plan"]
        plan = TerraformPlan(
            deployment_id=plan_data["deployment_id"],
            commit_sha=plan_data["commit_sha"],
            plan_hash=plan_data["plan_hash"],
            artifact=artifact_from(plan_data["artifact"]),
        )
        approval = DeploymentApproval(**fixture["approval"])

        self.assertEqual(snapshot.to_dict(), snapshot_data)
        self.assertEqual(patch.to_dict(), patch_data)
        self.assertTrue(approval.matches(plan))

    def test_approval_cannot_match_a_changed_plan(self) -> None:
        plan = TerraformPlan(
            deployment_id="deployment-001",
            commit_sha="commit-001",
            plan_hash="plan-hash-001",
            artifact=ArtifactReference(
                artifact_id="art-plan-001",
                artifact_type=ArtifactType.TERRAFORM_PLAN,
                content_sha256="plan-hash-001",
                customer_id="cust-001",
            ),
        )
        approval = DeploymentApproval(
            deployment_id="deployment-001",
            approved_by="user-001",
            commit_sha="commit-001",
            plan_hash="different-plan-hash",
        )

        self.assertFalse(approval.matches(plan))

    def test_aws_resource_contract_allows_only_read_operations(self) -> None:
        query = AwsResourceQuery(
            customer_id="cust-001",
            aws_account_id="123456789012",
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type="AWS::S3::Bucket",
            resource_id="logs-bucket",
        )

        self.assertEqual(query.to_dict()["operation"], "READ_RESOURCE")
        with self.assertRaisesRegex(ValueError, "READ_RESOURCE requires resource_id"):
            AwsResourceQuery(
                customer_id="cust-001",
                aws_account_id="123456789012",
                operation=AwsResourceOperation.READ_RESOURCE,
                resource_type="AWS::S3::Bucket",
            )

    def test_patch_rejects_paths_outside_the_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            RemediationPatch(
                finding_id="finding-001",
                base_commit_sha="commit-001",
                artifact=ArtifactReference(
                    artifact_id="art-patch-001",
                    artifact_type=ArtifactType.REMEDIATION_PATCH,
                    content_sha256="patch-hash-001",
                    customer_id="cust-001",
                ),
                changed_paths=("../outside.tf",),
            )


if __name__ == "__main__":
    unittest.main()
