"""M3 D 실행 port와 결정적 Mock 어댑터에 대한 Unit 테스트."""

import unittest
from types import MappingProxyType

from agent.runtime import (
    ActualRereadPort,
    ApplyDispatchPort,
    DeploymentPortError,
    DeploymentPortScopeError,
    MockActualRereadPort,
    MockApplyDispatchPort,
    MockPlanRequestPort,
    MockWorkflowRunReader,
    PlanRequestPort,
    WorkflowRunReader,
)
from packages.contracts import (
    ApplyRunReference,
    ArtifactReference,
    ArtifactType,
    AwsResourceSnapshot,
    DeploymentApproval,
    PlanReadinessInput,
    PlanRequestOutcome,
    TerraformPlan,
    TerraformStateVersion,
    VerifiedRunOutcome,
)

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
AWS_ACCOUNT_ID = "111122223333"
DEPLOYMENT_ID = "dep-abc123"
PLAN_HASH = "f" * 64
COMMIT = "a" * 40
LINEAGE = "11111111-2222-3333-4444-555555555555"


def build_approval(
    *,
    deployment_id: str = DEPLOYMENT_ID,
    plan_hash: str = PLAN_HASH,
    commit_sha: str = COMMIT,
) -> DeploymentApproval:
    return DeploymentApproval(
        deployment_id=deployment_id,
        approved_by="admin-1",
        commit_sha=commit_sha,
        plan_hash=plan_hash,
    )


# --- 반환형 Contract 테스트 -------------------------------------------------


class ReturnTypeContractTest(unittest.TestCase):
    def test_apply_run_reference_round_trip(self) -> None:
        reference = ApplyRunReference(
            deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, run_id="run-1"
        )
        self.assertEqual(
            reference.to_dict(),
            {
                "deployment_id": DEPLOYMENT_ID,
                "repository_id": REPOSITORY_ID,
                "run_id": "run-1",
            },
        )

    def test_apply_run_reference_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            ApplyRunReference(deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, run_id="")

    def test_apply_run_reference_is_frozen(self) -> None:
        reference = ApplyRunReference(
            deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, run_id="run-1"
        )
        with self.assertRaises(AttributeError):
            reference.run_id = "run-2"  # type: ignore[misc]

    def test_verified_run_outcome_succeeded_only_on_success(self) -> None:
        success = VerifiedRunOutcome(
            run_id="run-1",
            workflow_path="ci/terraform/apply.yml",
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="success",
            plan_hash=PLAN_HASH,
        )
        failure = VerifiedRunOutcome(
            run_id="run-2",
            workflow_path="ci/terraform/apply.yml",
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="failure",
            plan_hash=PLAN_HASH,
        )
        self.assertTrue(success.succeeded)
        self.assertFalse(failure.succeeded)

    def test_verified_run_outcome_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            VerifiedRunOutcome(
                run_id="run-1",
                workflow_path="",
                repository_id=REPOSITORY_ID,
                ref=COMMIT,
                conclusion="success",
                plan_hash=PLAN_HASH,
            )

    def test_aws_resource_snapshot_attributes_frozen(self) -> None:
        snapshot = AwsResourceSnapshot(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="bucket-001",
            attributes={"encryption": "aws:kms"},
        )
        self.assertIsInstance(snapshot.attributes, MappingProxyType)
        with self.assertRaises(TypeError):
            snapshot.attributes["encryption"] = "none"  # type: ignore[index]

    def test_aws_resource_snapshot_rejects_non_string_values(self) -> None:
        with self.assertRaises(TypeError):
            AwsResourceSnapshot(
                customer_id=CUSTOMER_ID,
                aws_account_id=AWS_ACCOUNT_ID,
                resource_type="AWS::S3::Bucket",
                resource_id="bucket-001",
                attributes={"versioning": True},  # type: ignore[dict-item]
            )

    def test_aws_resource_snapshot_round_trip(self) -> None:
        snapshot = AwsResourceSnapshot(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_type="AWS::S3::Bucket",
            resource_id="bucket-001",
            attributes={"encryption": "aws:kms"},
        )
        self.assertEqual(
            snapshot.to_dict(),
            {
                "customer_id": CUSTOMER_ID,
                "aws_account_id": AWS_ACCOUNT_ID,
                "resource_type": "AWS::S3::Bucket",
                "resource_id": "bucket-001",
                "attributes": {"encryption": "aws:kms"},
            },
        )


# --- ApplyDispatchPort -----------------------------------------------------


class ApplyDispatchPortTest(unittest.TestCase):
    def test_protocol_conformance(self) -> None:
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        self.assertIsInstance(tool, ApplyDispatchPort)

    def test_dispatch_returns_bound_reference(self) -> None:
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        reference = tool.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=7,
            repository_id=REPOSITORY_ID,
        )
        self.assertEqual(reference.deployment_id, DEPLOYMENT_ID)
        self.assertEqual(reference.repository_id, REPOSITORY_ID)

    def test_dispatch_is_idempotent(self) -> None:
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        first = tool.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=7,
            repository_id=REPOSITORY_ID,
        )
        second = tool.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=7,
            repository_id=REPOSITORY_ID,
        )
        self.assertEqual(first, second)

    def test_different_plan_hash_yields_different_run(self) -> None:
        # 서로 다른 deployment는 서로 다른 run을 얻는다(결정적 유도).
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        first = tool.dispatch_apply(
            approval=build_approval(deployment_id="dep-1", plan_hash="1" * 64),
            state_lineage=LINEAGE,
            state_serial=1,
            repository_id=REPOSITORY_ID,
        )
        second = tool.dispatch_apply(
            approval=build_approval(deployment_id="dep-2", plan_hash="2" * 64),
            state_lineage=LINEAGE,
            state_serial=1,
            repository_id=REPOSITORY_ID,
        )
        self.assertNotEqual(first.run_id, second.run_id)

    def test_dispatch_rejects_out_of_scope_repository(self) -> None:
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        with self.assertRaises(DeploymentPortScopeError):
            tool.dispatch_apply(
                approval=build_approval(),
                state_lineage=LINEAGE,
                state_serial=7,
                repository_id="repo-other",
            )

    def test_dispatch_rejects_bool_serial(self) -> None:
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        with self.assertRaises(TypeError):
            tool.dispatch_apply(
                approval=build_approval(),
                state_lineage=LINEAGE,
                state_serial=True,  # type: ignore[arg-type]
                repository_id=REPOSITORY_ID,
            )

    def test_dispatch_rejects_non_approval(self) -> None:
        tool = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        with self.assertRaises(TypeError):
            tool.dispatch_apply(
                approval="not-an-approval",  # type: ignore[arg-type]
                state_lineage=LINEAGE,
                state_serial=7,
                repository_id=REPOSITORY_ID,
            )


# --- WorkflowRunReader -----------------------------------------------------


class WorkflowRunReaderTest(unittest.TestCase):
    def test_protocol_conformance(self) -> None:
        reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        self.assertIsInstance(reader, WorkflowRunReader)

    def test_registered_run_is_returned(self) -> None:
        reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        outcome = VerifiedRunOutcome(
            run_id="run-1",
            workflow_path="ci/terraform/apply.yml",
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="success",
            plan_hash=PLAN_HASH,
        )
        reader.register_run(outcome)
        self.assertEqual(
            reader.read_run(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, run_id="run-1"),
            outcome,
        )

    def test_missing_run_returns_failure_value(self) -> None:
        # 미등록 run은 예외가 아니라 실패 결론을 값으로 반환한다.
        reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        outcome = reader.read_run(
            customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, run_id="run-unknown"
        )
        self.assertIsInstance(outcome, VerifiedRunOutcome)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.conclusion, "not_found")

    def test_read_run_rejects_out_of_scope(self) -> None:
        reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        with self.assertRaises(DeploymentPortScopeError):
            reader.read_run(customer_id="cust-other", repository_id=REPOSITORY_ID, run_id="run-1")

    def test_register_rejects_out_of_scope_repository(self) -> None:
        reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        with self.assertRaises(DeploymentPortScopeError):
            reader.register_run(
                VerifiedRunOutcome(
                    run_id="run-1",
                    workflow_path="ci/terraform/apply.yml",
                    repository_id="repo-other",
                    ref=COMMIT,
                    conclusion="success",
                    plan_hash=PLAN_HASH,
                )
            )

    def test_register_rejects_duplicate_run(self) -> None:
        reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        outcome = VerifiedRunOutcome(
            run_id="run-1",
            workflow_path="ci/terraform/apply.yml",
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="success",
            plan_hash=PLAN_HASH,
        )
        reader.register_run(outcome)
        with self.assertRaises(ValueError):
            reader.register_run(outcome)


# --- ActualRereadPort ------------------------------------------------------


def build_snapshot(resource_id: str) -> AwsResourceSnapshot:
    return AwsResourceSnapshot(
        customer_id=CUSTOMER_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        resource_type="AWS::S3::Bucket",
        resource_id=resource_id,
        attributes={"encryption": "aws:kms"},
    )


class ActualRereadPortTest(unittest.TestCase):
    def test_protocol_conformance(self) -> None:
        port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
        self.assertIsInstance(port, ActualRereadPort)

    def test_reread_narrows_to_requested_ids_in_order(self) -> None:
        port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
        port.register_snapshot(build_snapshot("bucket-a"))
        port.register_snapshot(build_snapshot("bucket-b"))
        port.register_snapshot(build_snapshot("bucket-c"))
        result = port.reread_actual(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_ids=("bucket-c", "bucket-a"),
        )
        self.assertEqual([s.resource_id for s in result], ["bucket-c", "bucket-a"])

    def test_reread_skips_unregistered_ids(self) -> None:
        port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
        port.register_snapshot(build_snapshot("bucket-a"))
        result = port.reread_actual(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_ids=("bucket-a", "bucket-missing"),
        )
        self.assertEqual([s.resource_id for s in result], ["bucket-a"])

    def test_reread_rejects_out_of_scope(self) -> None:
        port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
        with self.assertRaises(DeploymentPortScopeError):
            port.reread_actual(
                customer_id=CUSTOMER_ID,
                aws_account_id="999988887777",
                resource_ids=("bucket-a",),
            )

    def test_register_rejects_out_of_scope_snapshot(self) -> None:
        port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
        with self.assertRaises(DeploymentPortScopeError):
            port.register_snapshot(
                AwsResourceSnapshot(
                    customer_id="cust-other",
                    aws_account_id=AWS_ACCOUNT_ID,
                    resource_type="AWS::S3::Bucket",
                    resource_id="bucket-a",
                    attributes={"encryption": "aws:kms"},
                )
            )

    def test_reread_rejects_empty_resource_id(self) -> None:
        port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
        with self.assertRaises(ValueError):
            port.reread_actual(
                customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID, resource_ids=("",)
            )


# --- TerraformStateVersion -------------------------------------------------


class TerraformStateVersionTest(unittest.TestCase):
    def test_matches_requires_both_lineage_and_serial(self) -> None:
        base = TerraformStateVersion(lineage=LINEAGE, serial=7)
        self.assertTrue(base.matches(TerraformStateVersion(lineage=LINEAGE, serial=7)))
        # 같은 serial이라도 lineage가 다르면 다른 state다(state 재생성).
        self.assertFalse(base.matches(TerraformStateVersion(lineage="0" * 36, serial=7)))
        self.assertFalse(base.matches(TerraformStateVersion(lineage=LINEAGE, serial=8)))

    def test_rejects_bool_serial(self) -> None:
        with self.assertRaises(TypeError):
            TerraformStateVersion(lineage=LINEAGE, serial=True)  # type: ignore[arg-type]

    def test_rejects_negative_serial(self) -> None:
        with self.assertRaises(ValueError):
            TerraformStateVersion(lineage=LINEAGE, serial=-1)


# --- PlanRequestPort -------------------------------------------------------


def build_plan(
    *, deployment_id: str = DEPLOYMENT_ID, commit_sha: str = COMMIT, plan_hash: str = PLAN_HASH
) -> TerraformPlan:
    return TerraformPlan(
        deployment_id=deployment_id,
        commit_sha=commit_sha,
        plan_hash=plan_hash,
        artifact=ArtifactReference(
            artifact_id="art-plan-1",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256=plan_hash,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
    )


def build_outcome(
    *,
    deployment_id: str = DEPLOYMENT_ID,
    commit_sha: str = COMMIT,
    has_destructive_changes: bool = False,
) -> PlanRequestOutcome:
    plan = build_plan(deployment_id=deployment_id, commit_sha=commit_sha)
    return PlanRequestOutcome(
        plan=plan,
        state_version=TerraformStateVersion(lineage=LINEAGE, serial=7),
        readiness_input=PlanReadinessInput(
            plan=plan,
            refreshed=True,
            has_destructive_changes=has_destructive_changes,
            mapped_resource_ids=("bucket-a",),
        ),
    )


class PlanRequestOutcomeContractTest(unittest.TestCase):
    def test_rejects_readiness_input_for_a_different_plan(self) -> None:
        plan = build_plan()
        other_plan = build_plan(plan_hash="0" * 64)
        with self.assertRaisesRegex(ValueError, "same plan"):
            PlanRequestOutcome(
                plan=plan,
                state_version=TerraformStateVersion(lineage=LINEAGE, serial=1),
                readiness_input=PlanReadinessInput(
                    plan=other_plan,
                    refreshed=True,
                    has_destructive_changes=False,
                    mapped_resource_ids=("bucket-a",),
                ),
            )

    def test_round_trip_exposes_plan_and_state(self) -> None:
        outcome = build_outcome()
        data = outcome.to_dict()
        self.assertEqual(data["state_version"], {"lineage": LINEAGE, "serial": 7})
        self.assertEqual(data["plan"]["plan_hash"], PLAN_HASH)


class PlanRequestPortTest(unittest.TestCase):
    def test_protocol_conformance(self) -> None:
        port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        self.assertIsInstance(port, PlanRequestPort)

    def test_registered_plan_is_returned(self) -> None:
        port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        outcome = build_outcome()
        port.register_plan(deployment_id=DEPLOYMENT_ID, commit_sha=COMMIT, outcome=outcome)
        result = port.request_plan(
            customer_id=CUSTOMER_ID,
            deployment_id=DEPLOYMENT_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT,
        )
        self.assertEqual(result, outcome)

    def test_request_is_deterministic(self) -> None:
        port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        port.register_plan(deployment_id=DEPLOYMENT_ID, commit_sha=COMMIT, outcome=build_outcome())
        first = port.request_plan(
            customer_id=CUSTOMER_ID,
            deployment_id=DEPLOYMENT_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT,
        )
        second = port.request_plan(
            customer_id=CUSTOMER_ID,
            deployment_id=DEPLOYMENT_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT,
        )
        self.assertEqual(first, second)

    def test_unregistered_plan_is_an_error_not_a_value(self) -> None:
        # plan 부재는 흐름이 성립하지 않는 오류다(재조회 실패와 다르다).
        port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        with self.assertRaises(DeploymentPortError):
            port.request_plan(
                customer_id=CUSTOMER_ID,
                deployment_id=DEPLOYMENT_ID,
                repository_id=REPOSITORY_ID,
                commit_sha=COMMIT,
            )

    def test_request_rejects_out_of_scope(self) -> None:
        port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        with self.assertRaises(DeploymentPortScopeError):
            port.request_plan(
                customer_id="cust-other",
                deployment_id=DEPLOYMENT_ID,
                repository_id=REPOSITORY_ID,
                commit_sha=COMMIT,
            )

    def test_register_rejects_mismatched_coordinates(self) -> None:
        port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        with self.assertRaises(ValueError):
            port.register_plan(
                deployment_id="dep-other", commit_sha=COMMIT, outcome=build_outcome()
            )


if __name__ == "__main__":
    unittest.main()
