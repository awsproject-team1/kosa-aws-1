"""D execution ports let A/C build against fixtures before D's live adapters (ADR-0019)."""

import unittest

from apps.backend.deployment import (
    ActualRereadPort,
    ApplyDispatchPort,
    PlanRequestPort,
    WorkflowRunReader,
)
from packages.contracts import (
    ApplyDispatchReceipt,
    ArtifactReference,
    ArtifactType,
    DeploymentApproval,
    PlanExecutionResult,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
)
from packages.contracts.remediation import RemediationSyncTarget


def _plan() -> TerraformPlan:
    return TerraformPlan(
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


class FakePlanRequest:
    def request_plan(
        self, *, customer_id: str, deployment_id: str, repository_id: str, commit_sha: str
    ) -> PlanExecutionResult:
        return PlanExecutionResult(
            plan=_plan(),
            binary_artifact=ArtifactReference(
                artifact_id="art-plan-bin-001",
                artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
                content_sha256="binary-digest-001",
                customer_id=customer_id,
            ),
            state_version=TerraformStateVersion(lineage="lineage-1", serial=3),
        )


class FakeApplyDispatch:
    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        plan: TerraformPlan,
        state_version: TerraformStateVersion,
    ) -> ApplyDispatchReceipt:
        return ApplyDispatchReceipt(
            deployment_id=plan.deployment_id,
            repository_id="repo-001",
            workflow_path=".github/workflows/apply.yml",
        )


class FakeWorkflowRunReader:
    def read_run(self, reference: WorkflowRunReference) -> WorkflowRunFacts:
        return WorkflowRunFacts(
            run_id=reference.run_id,
            repository_id=reference.repository_id,
            workflow_path=".github/workflows/apply.yml",
            ref="refs/heads/main",
            commit_sha="commit-001",
            conclusion=WorkflowConclusion.SUCCESS,
            plan_hash="plan-hash-001",
        )


class FakeActualReread:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reread_actual(
        self, *, customer_id: str, deployment_id: str, sync_target: RemediationSyncTarget
    ) -> None:
        self.calls.append(deployment_id)


class DeploymentPortsTest(unittest.TestCase):
    def test_fixtures_satisfy_the_port_protocols(self) -> None:
        self.assertIsInstance(FakePlanRequest(), PlanRequestPort)
        self.assertIsInstance(FakeApplyDispatch(), ApplyDispatchPort)
        self.assertIsInstance(FakeWorkflowRunReader(), WorkflowRunReader)
        self.assertIsInstance(FakeActualReread(), ActualRereadPort)

    def test_plan_request_returns_hashed_plan_and_state(self) -> None:
        result = FakePlanRequest().request_plan(
            customer_id="cust-001",
            deployment_id="deployment-001",
            repository_id="repo-001",
            commit_sha="commit-001",
        )
        self.assertEqual(result.plan.plan_hash, "plan-hash-001")
        self.assertEqual(result.state_version.serial, 3)
        self.assertIs(result.binary_artifact.artifact_type, ArtifactType.TERRAFORM_PLAN_BINARY)

    def test_run_reader_returns_verifiable_facts(self) -> None:
        facts = FakeWorkflowRunReader().read_run(
            WorkflowRunReference(
                deployment_id="deployment-001", repository_id="repo-001", run_id="run-1"
            )
        )
        self.assertEqual(facts.commit_sha, "commit-001")
        self.assertIs(facts.conclusion, WorkflowConclusion.SUCCESS)

    def test_actual_reread_is_invoked_with_deployment(self) -> None:
        reread = FakeActualReread()
        reread.reread_actual(
            customer_id="cust-001",
            deployment_id="deployment-001",
            sync_target=RemediationSyncTarget(
                finding_id="finding-001",
                customer_id="cust-001",
                repository_id="repo-001",
                commit_sha="commit-001",
            ),
        )
        self.assertEqual(reread.calls, ["deployment-001"])


if __name__ == "__main__":
    unittest.main()
