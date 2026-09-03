"""Contract tests for the GitHub/AWS read-only and approval boundaries."""

import json
import unittest
from pathlib import Path

from packages.contracts import (
    ApplyDispatchReceipt,
    ArtifactReference,
    ArtifactType,
    AwsResourceOperation,
    AwsResourceQuery,
    DeploymentApproval,
    IaCSnapshot,
    PlanExecutionResult,
    PlanSummary,
    RemediationPatch,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
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


class TerraformStateVersionContractTest(unittest.TestCase):
    def test_matches_requires_same_lineage_and_serial(self) -> None:
        version = TerraformStateVersion(lineage="lineage-1", serial=7)
        self.assertTrue(version.matches(TerraformStateVersion(lineage="lineage-1", serial=7)))
        # Same serial, different lineage: a re-created state must not match.
        self.assertFalse(version.matches(TerraformStateVersion(lineage="lineage-2", serial=7)))
        self.assertFalse(version.matches(TerraformStateVersion(lineage="lineage-1", serial=8)))

    def test_serial_rejects_bool_and_negative(self) -> None:
        with self.assertRaises(TypeError):
            TerraformStateVersion(lineage="lineage-1", serial=True)
        with self.assertRaises(ValueError):
            TerraformStateVersion(lineage="lineage-1", serial=-1)


def _summary(**overrides: object) -> PlanSummary:
    values: dict[str, object] = {
        "refreshed": True,
        "has_destructive_changes": False,
        "mapped_resource_ids": ("bucket-public-001",),
    }
    values.update(overrides)
    return PlanSummary(**values)  # type: ignore[arg-type]


class PlanSummaryContractTest(unittest.TestCase):
    """C readiness가 소비하는 D의 plan 요약 (ADR-0019 §1 addendum)."""

    def test_serializes_the_three_readiness_facts(self) -> None:
        self.assertEqual(
            _summary().to_dict(),
            {
                "refreshed": True,
                "has_destructive_changes": False,
                "mapped_resource_ids": ["bucket-public-001"],
            },
        )

    def test_rejects_non_boolean_flags(self) -> None:
        for name in ("refreshed", "has_destructive_changes"):
            with self.assertRaises(TypeError):
                _summary(**{name: "true"})

    def test_rejects_duplicate_resource_ids(self) -> None:
        """중복은 "몇 개를 건드리는가"를 흐리고 저장 왕복에서 순서만 다른 값을 만든다."""
        with self.assertRaises(ValueError):
            _summary(mapped_resource_ids=("bucket-a", "bucket-a"))

    def test_allows_an_empty_mapping(self) -> None:
        """어떤 리소스도 매핑되지 않는 것은 정상 값이다 — readiness가 BLOCKED로 판정한다."""
        self.assertEqual(_summary(mapped_resource_ids=()).mapped_resource_ids, ())


class PlanExecutionResultContractTest(unittest.TestCase):
    def _plan(self, *, repository_id: str | None = None) -> TerraformPlan:
        return TerraformPlan(
            deployment_id="deployment-001",
            commit_sha="commit-001",
            plan_hash="plan-hash-001",
            artifact=ArtifactReference(
                artifact_id="art-plan-001",
                artifact_type=ArtifactType.TERRAFORM_PLAN,
                content_sha256="plan-hash-001",
                customer_id="cust-001",
                repository_id=repository_id,
            ),
        )

    def _binary(
        self,
        artifact_type: ArtifactType = ArtifactType.TERRAFORM_PLAN_BINARY,
        *,
        customer_id: str = "cust-001",
        repository_id: str | None = None,
    ) -> ArtifactReference:
        return ArtifactReference(
            artifact_id="art-plan-bin-001",
            artifact_type=artifact_type,
            content_sha256="binary-digest-001",
            customer_id=customer_id,
            repository_id=repository_id,
        )

    @staticmethod
    def _plan_run(
        *, deployment_id: str = "deployment-001", repository_id: str = "repo-001"
    ) -> WorkflowRunReference:
        return WorkflowRunReference(
            deployment_id=deployment_id, repository_id=repository_id, run_id="plan-run-1"
        )

    def test_bundles_plan_binary_and_state(self) -> None:
        result = PlanExecutionResult(
            plan=self._plan(),
            binary_artifact=self._binary(),
            state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
            summary=_summary(),
            plan_run=self._plan_run(),
        )
        payload = result.to_dict()
        self.assertEqual(payload["plan"]["plan_hash"], "plan-hash-001")
        self.assertEqual(payload["state_version"], {"lineage": "lineage-1", "serial": 3})
        self.assertEqual(payload["plan_run"]["run_id"], "plan-run-1")

    def test_rejects_a_plan_run_from_a_different_repository(self) -> None:
        """binary가 저장소를 밝히면 plan run도 같은 저장소여야 한다.

        다르면 apply가 다른 저장소의 run에서 plan artifact를 내려받으면서도 `deployment_id`는
        일치하는 상태가 된다.
        """
        with self.assertRaisesRegex(ValueError, "plan_run repository_id"):
            PlanExecutionResult(
                plan=self._plan(repository_id="repo-001"),
                binary_artifact=self._binary(repository_id="repo-001"),
                state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
                summary=_summary(),
                plan_run=self._plan_run(repository_id="repo-other"),
            )

    def test_rejects_a_plan_run_from_a_different_deployment(self) -> None:
        """apply는 이 run의 artifact를 내려받으므로, 다른 배포의 run이면 다른 plan을 적용한다."""
        with self.assertRaisesRegex(ValueError, "plan_run deployment_id"):
            PlanExecutionResult(
                plan=self._plan(),
                binary_artifact=self._binary(),
                state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
                summary=_summary(),
                plan_run=self._plan_run(deployment_id="dep-other"),
            )

    def test_rejects_non_binary_artifact_as_the_saved_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "TERRAFORM_PLAN_BINARY"):
            PlanExecutionResult(
                plan=self._plan(),
                binary_artifact=self._binary(ArtifactType.TERRAFORM_PLAN),
                state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
                summary=_summary(),
                plan_run=self._plan_run(),
            )

    def test_rejects_binary_from_a_different_customer(self) -> None:
        with self.assertRaisesRegex(ValueError, "customer_id"):
            PlanExecutionResult(
                plan=self._plan(),
                binary_artifact=self._binary(customer_id="cust-other"),
                state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
                summary=_summary(),
                plan_run=self._plan_run(),
            )

    def test_rejects_binary_from_a_different_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "repository_id"):
            PlanExecutionResult(
                plan=self._plan(),
                binary_artifact=self._binary(repository_id="repo-other"),
                state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
                summary=_summary(),
                plan_run=self._plan_run(),
            )


class WorkflowRunFactsContractTest(unittest.TestCase):
    def test_run_facts_round_trip_with_conclusion(self) -> None:
        facts = WorkflowRunFacts(
            run_id="run-001",
            repository_id="repo-001",
            workflow_path=".github/workflows/apply.yml",
            ref="refs/heads/main",
            commit_sha="commit-001",
            conclusion=WorkflowConclusion.SUCCESS,
            plan_hash="plan-hash-001",
        )
        self.assertEqual(facts.to_dict()["conclusion"], "SUCCESS")

    def test_run_reference_and_receipt_require_non_empty_fields(self) -> None:
        WorkflowRunReference(deployment_id="d-1", repository_id="r-1", run_id="run-1")
        ApplyDispatchReceipt(
            deployment_id="d-1", repository_id="r-1", workflow_path=".github/workflows/apply.yml"
        )
        with self.assertRaises(ValueError):
            WorkflowRunReference(deployment_id="", repository_id="r-1", run_id="run-1")


if __name__ == "__main__":
    unittest.main()
