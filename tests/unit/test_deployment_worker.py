"""D Deployment Worker의 revision-bound 실행 경로 Unit 테스트 (ADR-0019).

고정하는 불변식:
- command당 정확히 하나의 injected D port를 호출한다 (RUN_DEPLOYMENT/PLAN_COMPLETED/APPLY_COMPLETED).
- work는 queue payload가 아니라 (job_id, revision)으로 다시 읽고, 불일치하면 진행하지 않는다.
- 승인되지 않은/불일치 plan으로는 apply를 dispatch하지 않는다.
- 재조회한 run이 승인 사실(repository/workflow_path/ref/plan_hash/conclusion)과 하나라도 다르면
  apply 후 Actual을 재조회하지 않고 차단한다 (EventBridge payload만으로 상태가 확정되지 않는다).
- 같은 approval로 두 번째 apply run을 만들지 않는다 (idempotent dispatch).
"""

import unittest

from agent.runtime import (
    DeploymentPortError,
    MockActualRereadPort,
    MockApplyDispatchPort,
    MockPlanRequestPort,
    MockWorkflowRunReader,
)
from apps.backend.deployment import (
    DeploymentApplyBlockedError,
    DeploymentWork,
    DeploymentWorker,
    DeploymentWorkerError,
    DeploymentWorkNotFoundError,
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
    WorkflowCommand,
    WorkflowTask,
)

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
AWS_ACCOUNT_ID = "111122223333"
DEPLOYMENT_ID = "dep-abc123"
JOB_ID = "job-dep-1"
PLAN_HASH = "f" * 64
COMMIT = "a" * 40
LINEAGE = "11111111-2222-3333-4444-555555555555"
SERIAL = 7
APPLY_WORKFLOW = "ci/terraform/apply.yml"
RESOURCE_ID = "logs-bucket"


def build_plan() -> TerraformPlan:
    return TerraformPlan(
        deployment_id=DEPLOYMENT_ID,
        commit_sha=COMMIT,
        plan_hash=PLAN_HASH,
        artifact=ArtifactReference(
            artifact_id="art-plan-1",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256=PLAN_HASH,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
    )


def build_outcome() -> PlanRequestOutcome:
    plan = build_plan()
    return PlanRequestOutcome(
        plan=plan,
        state_version=TerraformStateVersion(lineage=LINEAGE, serial=SERIAL),
        readiness_input=PlanReadinessInput(
            plan=plan,
            refreshed=True,
            has_destructive_changes=False,
            mapped_resource_ids=(RESOURCE_ID,),
        ),
    )


def build_approval() -> DeploymentApproval:
    return DeploymentApproval(
        deployment_id=DEPLOYMENT_ID,
        approved_by="admin-1",
        commit_sha=COMMIT,
        plan_hash=PLAN_HASH,
    )


def build_snapshot() -> AwsResourceSnapshot:
    return AwsResourceSnapshot(
        customer_id=CUSTOMER_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        resource_type="AWS::S3::Bucket",
        resource_id=RESOURCE_ID,
        attributes={"encryption": "aws:kms"},
    )


class FakeWorkRepository:
    def __init__(self, work: DeploymentWork | None) -> None:
        self._work = work

    def get_work(self, *, job_id: str, expected_revision: int) -> DeploymentWork | None:
        if self._work is None:
            return None
        if self._work.job_id != job_id or self._work.revision != expected_revision:
            return None
        return self._work


class RecordingStore:
    def __init__(self) -> None:
        self.plans: list[PlanRequestOutcome] = []
        self.runs: list[ApplyRunReference] = []
        self.verifications: list[tuple[VerifiedRunOutcome, tuple[AwsResourceSnapshot, ...]]] = []

    def put_plan_if_absent(self, *, work: DeploymentWork, outcome: PlanRequestOutcome) -> None:
        self.plans.append(outcome)

    def put_run_if_absent(self, *, work: DeploymentWork, reference: ApplyRunReference) -> None:
        self.runs.append(reference)

    def put_verification_if_absent(self, *, work, outcome, actual) -> None:
        self.verifications.append((outcome, actual))


def build_worker(
    *,
    work: DeploymentWork | None,
    run_outcome: VerifiedRunOutcome | None = None,
    snapshots: tuple[AwsResourceSnapshot, ...] = (),
    plan_registered: bool = True,
) -> tuple[DeploymentWorker, RecordingStore, MockApplyDispatchPort]:
    plan_port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
    if plan_registered:
        plan_port.register_plan(
            deployment_id=DEPLOYMENT_ID, commit_sha=COMMIT, outcome=build_outcome()
        )
    apply_port = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
    run_reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
    if run_outcome is not None:
        run_reader.register_run(run_outcome)
    actual_port = MockActualRereadPort(customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID)
    for snapshot in snapshots:
        actual_port.register_snapshot(snapshot)
    store = RecordingStore()
    worker = DeploymentWorker(
        work_repository=FakeWorkRepository(work),
        plan_port=plan_port,
        apply_port=apply_port,
        run_reader=run_reader,
        actual_port=actual_port,
        plan_store=store,
        run_store=store,
        verification_store=store,
    )
    return worker, store, apply_port


def plan_work() -> DeploymentWork:
    """RUN_DEPLOYMENT 단계의 work — plan/approval 아직 없음."""
    return DeploymentWork(
        customer_id=CUSTOMER_ID,
        deployment_id=DEPLOYMENT_ID,
        repository_id=REPOSITORY_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        job_id=JOB_ID,
        revision=0,
        commit_sha=COMMIT,
        mapped_resource_ids=(RESOURCE_ID,),
    )


def approved_work(revision: int = 1) -> DeploymentWork:
    """apply 계열 command의 work — stored plan/approval/state 포함."""
    return DeploymentWork(
        customer_id=CUSTOMER_ID,
        deployment_id=DEPLOYMENT_ID,
        repository_id=REPOSITORY_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        job_id=JOB_ID,
        revision=revision,
        commit_sha=COMMIT,
        mapped_resource_ids=(RESOURCE_ID,),
        plan=build_plan(),
        state_version=TerraformStateVersion(lineage=LINEAGE, serial=SERIAL),
        approval=build_approval(),
    )


def success_run(run_id: str) -> VerifiedRunOutcome:
    return VerifiedRunOutcome(
        run_id=run_id,
        workflow_path=APPLY_WORKFLOW,
        repository_id=REPOSITORY_ID,
        ref=COMMIT,
        conclusion="success",
        plan_hash=PLAN_HASH,
    )


class DeploymentWorkerDependencyTest(unittest.TestCase):
    def test_requires_all_dependencies(self) -> None:
        with self.assertRaises(TypeError):
            DeploymentWorker(
                work_repository=FakeWorkRepository(None),
                plan_port=None,  # type: ignore[arg-type]
                apply_port=MockApplyDispatchPort(repository_id=REPOSITORY_ID),
                run_reader=MockWorkflowRunReader(
                    customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID
                ),
                actual_port=MockActualRereadPort(
                    customer_id=CUSTOMER_ID, aws_account_id=AWS_ACCOUNT_ID
                ),
                plan_store=RecordingStore(),
                run_store=RecordingStore(),
                verification_store=RecordingStore(),
            )

    def test_rejects_non_task(self) -> None:
        worker, _, _ = build_worker(work=plan_work())
        with self.assertRaises(TypeError):
            worker.handle("not-a-task")  # type: ignore[arg-type]


class ReloadTest(unittest.TestCase):
    def test_missing_work_is_not_found(self) -> None:
        worker, _, _ = build_worker(work=None)
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        with self.assertRaises(DeploymentWorkNotFoundError):
            worker.handle(task)

    def test_stale_revision_is_not_found(self) -> None:
        worker, _, _ = build_worker(work=plan_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=5, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        with self.assertRaises(DeploymentWorkNotFoundError):
            worker.handle(task)


class RunDeploymentTest(unittest.TestCase):
    def test_requests_plan_and_stores_it(self) -> None:
        worker, store, apply_port = build_worker(work=plan_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        outcome = worker.handle(task)
        self.assertIsInstance(outcome, PlanRequestOutcome)
        self.assertEqual(len(store.plans), 1)
        # RUN_DEPLOYMENT는 apply port를 건드리지 않는다.
        self.assertEqual(store.runs, [])

    def test_missing_registered_plan_raises(self) -> None:
        # plan 부재는 port 계층 오류다(D Worker는 재시도하지 않는다).
        worker, _, _ = build_worker(work=plan_work(), plan_registered=False)
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        with self.assertRaises(DeploymentPortError):
            worker.handle(task)


class DispatchApplyTest(unittest.TestCase):
    def test_dispatches_apply_for_approved_work(self) -> None:
        worker, store, _ = build_worker(work=approved_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.PLAN_COMPLETED
        )
        reference = worker.handle(task)
        self.assertIsInstance(reference, ApplyRunReference)
        self.assertEqual(reference.deployment_id, DEPLOYMENT_ID)
        self.assertEqual(len(store.runs), 1)

    def test_unapproved_work_does_not_dispatch(self) -> None:
        # plan/approval 없는 work로 PLAN_COMPLETED를 처리하면 apply를 dispatch하지 않는다.
        worker, store, _ = build_worker(work=plan_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.PLAN_COMPLETED
        )
        with self.assertRaises(DeploymentWorkerError):
            worker.handle(task)
        self.assertEqual(store.runs, [])

    def test_approval_not_matching_plan_is_rejected(self) -> None:
        mismatched = DeploymentWork(
            customer_id=CUSTOMER_ID,
            deployment_id=DEPLOYMENT_ID,
            repository_id=REPOSITORY_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            job_id=JOB_ID,
            revision=1,
            commit_sha=COMMIT,
            plan=build_plan(),
            state_version=TerraformStateVersion(lineage=LINEAGE, serial=SERIAL),
            approval=DeploymentApproval(
                deployment_id=DEPLOYMENT_ID,
                approved_by="admin-1",
                commit_sha=COMMIT,
                plan_hash="0" * 64,  # plan_hash가 plan과 다르다
            ),
        )
        worker, store, _ = build_worker(work=mismatched)
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.PLAN_COMPLETED
        )
        with self.assertRaises(DeploymentWorkerError):
            worker.handle(task)
        self.assertEqual(store.runs, [])


class VerifyApplyTest(unittest.TestCase):
    def _run_id_for_work(self) -> str:
        # dispatch가 결정적이므로 approved work가 얻을 run_id를 미리 계산한다.
        apply_port = MockApplyDispatchPort(repository_id=REPOSITORY_ID)
        return apply_port.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=SERIAL,
            repository_id=REPOSITORY_ID,
        ).run_id

    def test_verifies_and_rereads_actual_on_success(self) -> None:
        run_id = self._run_id_for_work()
        worker, store, _ = build_worker(
            work=approved_work(),
            run_outcome=success_run(run_id),
            snapshots=(build_snapshot(),),
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        outcome = worker.handle(task)
        self.assertIsInstance(outcome, VerifiedRunOutcome)
        self.assertTrue(outcome.succeeded)
        self.assertEqual(len(store.verifications), 1)
        _, actual = store.verifications[0]
        self.assertEqual([s.resource_id for s in actual], [RESOURCE_ID])

    def test_unregistered_run_is_blocked(self) -> None:
        # 미등록 run은 not_found 값을 돌려주고, plan_hash가 어긋나 차단된다.
        worker, store, _ = build_worker(work=approved_work(), snapshots=(build_snapshot(),))
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)
        self.assertEqual(store.verifications, [])

    def test_wrong_plan_hash_run_is_blocked(self) -> None:
        run_id = self._run_id_for_work()
        wrong = VerifiedRunOutcome(
            run_id=run_id,
            workflow_path=APPLY_WORKFLOW,
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="success",
            plan_hash="0" * 64,  # 승인 plan_hash와 다르다
        )
        worker, store, _ = build_worker(
            work=approved_work(), run_outcome=wrong, snapshots=(build_snapshot(),)
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)
        self.assertEqual(store.verifications, [])

    def test_non_allowlisted_workflow_is_blocked(self) -> None:
        run_id = self._run_id_for_work()
        wrong = VerifiedRunOutcome(
            run_id=run_id,
            workflow_path="ci/terraform/malicious.yml",
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="success",
            plan_hash=PLAN_HASH,
        )
        worker, _, _ = build_worker(work=approved_work(), run_outcome=wrong)
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)

    def test_failed_conclusion_is_blocked(self) -> None:
        run_id = self._run_id_for_work()
        failed = VerifiedRunOutcome(
            run_id=run_id,
            workflow_path=APPLY_WORKFLOW,
            repository_id=REPOSITORY_ID,
            ref=COMMIT,
            conclusion="failure",
            plan_hash=PLAN_HASH,
        )
        worker, _, _ = build_worker(work=approved_work(), run_outcome=failed)
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)


if __name__ == "__main__":
    unittest.main()
