"""D Deployment Worker의 revision-bound 실행 경로 Unit 테스트 (ADR-0019).

고정하는 불변식:
- command당 정확히 하나의 injected D port를 호출한다(RUN_DEPLOYMENT/PLAN_COMPLETED/APPLY_COMPLETED).
- work는 queue payload가 아니라 (job_id, revision)으로 다시 읽고, 불일치하면 진행하지 않는다.
- 승인되지 않은/불일치 plan으로는 apply를 dispatch하지 않는다.
- APPLY_COMPLETED는 apply를 재dispatch하지 않고 **저장된 run reference**로 재조회한다(P1-1, §5·§7).
- 재조회한 run이 승인 사실(repository/workflow_path/ref/commit/plan_hash/conclusion)과 하나라도
  다르면 apply 후 Actual을 재조회하지 않고 차단한다.
"""

import unittest

from agent.runtime import (
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
    ApplyDispatchReceipt,
    ArtifactReference,
    ArtifactType,
    DeploymentApproval,
    PlanExecutionResult,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowCommand,
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
    WorkflowTask,
)
from packages.contracts.remediation import RemediationSyncTarget

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
AWS_ACCOUNT_ID = "111122223333"
DEPLOYMENT_ID = "dep-abc123"
JOB_ID = "job-dep-1"
PLAN_HASH = "f" * 64
COMMIT = "a" * 40
LINEAGE = "11111111-2222-3333-4444-555555555555"
SERIAL = 7
APPLY_WORKFLOW = ".github/workflows/terraform-apply.yml"
RUN_ID = "run-777"
PLAN_RUN_ID = "plan-run-555"


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


def build_binary() -> ArtifactReference:
    return ArtifactReference(
        artifact_id="art-plan-bin-1",
        artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
        content_sha256="b" * 64,
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
    )


def build_plan_run() -> WorkflowRunReference:
    return WorkflowRunReference(
        deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, run_id=PLAN_RUN_ID
    )


def build_plan_result() -> PlanExecutionResult:
    return PlanExecutionResult(
        plan=build_plan(),
        binary_artifact=build_binary(),
        state_version=TerraformStateVersion(lineage=LINEAGE, serial=SERIAL),
        plan_run=build_plan_run(),
    )


def build_approval() -> DeploymentApproval:
    return DeploymentApproval(
        deployment_id=DEPLOYMENT_ID, approved_by="admin-1", commit_sha=COMMIT, plan_hash=PLAN_HASH
    )


def build_sync_target() -> RemediationSyncTarget:
    return RemediationSyncTarget(
        finding_id="find-1", customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, commit_sha=COMMIT
    )


def success_facts(run_id: str = RUN_ID) -> WorkflowRunFacts:
    return WorkflowRunFacts(
        run_id=run_id,
        repository_id=REPOSITORY_ID,
        workflow_path=APPLY_WORKFLOW,
        ref=COMMIT,
        commit_sha=COMMIT,
        conclusion=WorkflowConclusion.SUCCESS,
        plan_hash=PLAN_HASH,
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
        self.plans: list[PlanExecutionResult] = []
        self.receipts: list[ApplyDispatchReceipt] = []
        self.verifications: list[WorkflowRunFacts] = []

    def put_plan_if_absent(self, *, work: DeploymentWork, result: PlanExecutionResult) -> None:
        self.plans.append(result)

    def put_receipt_if_absent(self, *, work: DeploymentWork, receipt: ApplyDispatchReceipt) -> None:
        self.receipts.append(receipt)

    def put_verification_if_absent(self, *, work: DeploymentWork, facts: WorkflowRunFacts) -> None:
        self.verifications.append(facts)


def build_worker(
    *, work: DeploymentWork | None, run_facts: WorkflowRunFacts | None = None
) -> tuple[DeploymentWorker, RecordingStore, MockApplyDispatchPort, MockActualRereadPort]:
    plan_port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
    plan_port.register_plan(
        deployment_id=DEPLOYMENT_ID, commit_sha=COMMIT, result=build_plan_result()
    )
    apply_port = MockApplyDispatchPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
    run_reader = MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
    if run_facts is not None:
        run_reader.register_run(run_facts)
    actual_port = MockActualRereadPort(customer_id=CUSTOMER_ID)
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
    return worker, store, apply_port, actual_port


def plan_work() -> DeploymentWork:
    return DeploymentWork(
        customer_id=CUSTOMER_ID,
        deployment_id=DEPLOYMENT_ID,
        repository_id=REPOSITORY_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        job_id=JOB_ID,
        revision=0,
        commit_sha=COMMIT,
    )


def approved_work(
    *,
    revision: int = 1,
    run_reference: WorkflowRunReference | None = None,
    plan_run: WorkflowRunReference | None = None,
    include_plan_run: bool = True,
) -> DeploymentWork:
    return DeploymentWork(
        customer_id=CUSTOMER_ID,
        deployment_id=DEPLOYMENT_ID,
        repository_id=REPOSITORY_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        job_id=JOB_ID,
        revision=revision,
        commit_sha=COMMIT,
        plan=build_plan(),
        state_version=TerraformStateVersion(lineage=LINEAGE, serial=SERIAL),
        plan_run=(plan_run or build_plan_run()) if include_plan_run else None,
        approval=build_approval(),
        run_reference=run_reference,
        sync_target=build_sync_target(),
    )


def run_reference() -> WorkflowRunReference:
    return WorkflowRunReference(
        deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, run_id=RUN_ID
    )


class DependencyTest(unittest.TestCase):
    def test_rejects_non_task(self) -> None:
        worker, _, _, _ = build_worker(work=plan_work())
        with self.assertRaises(TypeError):
            worker.handle("not-a-task")  # type: ignore[arg-type]


class ReloadTest(unittest.TestCase):
    def test_missing_work_is_not_found(self) -> None:
        worker, _, _, _ = build_worker(work=None)
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        with self.assertRaises(DeploymentWorkNotFoundError):
            worker.handle(task)

    def test_stale_revision_is_not_found(self) -> None:
        worker, _, _, _ = build_worker(work=plan_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=5, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        with self.assertRaises(DeploymentWorkNotFoundError):
            worker.handle(task)


class RunDeploymentTest(unittest.TestCase):
    def test_requests_plan_and_stores_it(self) -> None:
        worker, store, _, _ = build_worker(work=plan_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
        )
        result = worker.handle(task)
        self.assertIsInstance(result, PlanExecutionResult)
        self.assertEqual(len(store.plans), 1)
        self.assertEqual(store.receipts, [])

    def test_plan_run_outside_the_work_is_not_stored(self) -> None:
        """다른 저장소의 run이 저장되면 apply가 그 run의 plan artifact를 적용하게 된다(§1).

        `PlanExecutionResult`는 binary가 `repository_id`를 가질 때만 그 값을 plan_run과 대조한다.
        repository_id 없는 artifact 조합에서는 Worker의 work scope 확인이 유일한 방어다.
        """
        unscoped_plan = TerraformPlan(
            deployment_id=DEPLOYMENT_ID,
            commit_sha=COMMIT,
            plan_hash=PLAN_HASH,
            artifact=ArtifactReference(
                artifact_id="art-plan-1",
                artifact_type=ArtifactType.TERRAFORM_PLAN,
                content_sha256=PLAN_HASH,
                customer_id=CUSTOMER_ID,
            ),
        )
        plan_port = MockPlanRequestPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID)
        plan_port.register_plan(
            deployment_id=DEPLOYMENT_ID,
            commit_sha=COMMIT,
            result=PlanExecutionResult(
                plan=unscoped_plan,
                binary_artifact=ArtifactReference(
                    artifact_id="art-plan-bin-1",
                    artifact_type=ArtifactType.TERRAFORM_PLAN_BINARY,
                    content_sha256="b" * 64,
                    customer_id=CUSTOMER_ID,
                ),
                state_version=TerraformStateVersion(lineage=LINEAGE, serial=SERIAL),
                plan_run=WorkflowRunReference(
                    deployment_id=DEPLOYMENT_ID,
                    repository_id="repo-other",
                    run_id=PLAN_RUN_ID,
                ),
            ),
        )
        store = RecordingStore()
        worker = DeploymentWorker(
            work_repository=FakeWorkRepository(plan_work()),
            plan_port=plan_port,
            apply_port=MockApplyDispatchPort(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID),
            run_reader=MockWorkflowRunReader(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID),
            actual_port=MockActualRereadPort(customer_id=CUSTOMER_ID),
            plan_store=store,
            run_store=store,
            verification_store=store,
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
        )

        with self.assertRaisesRegex(DeploymentWorkerError, "plan run is outside"):
            worker.handle(task)

        self.assertEqual(store.plans, [])


class DispatchApplyTest(unittest.TestCase):
    def test_dispatches_apply_for_approved_work(self) -> None:
        worker, store, apply_port, _ = build_worker(work=approved_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.PLAN_COMPLETED
        )
        receipt = worker.handle(task)
        self.assertIsInstance(receipt, ApplyDispatchReceipt)
        self.assertEqual(receipt.deployment_id, DEPLOYMENT_ID)
        self.assertEqual(len(store.receipts), 1)
        self.assertEqual(apply_port.dispatch_count, 1)

    def test_work_without_a_plan_run_does_not_dispatch(self) -> None:
        """apply는 plan run의 artifact를 내려받는다. run 좌표가 없으면 무엇을 적용할지 없다(§1)."""
        worker, store, apply_port, _ = build_worker(work=approved_work(include_plan_run=False))
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.PLAN_COMPLETED
        )

        with self.assertRaisesRegex(DeploymentWorkerError, "no plan run"):
            worker.handle(task)

        self.assertEqual(store.receipts, [])
        self.assertEqual(apply_port.dispatch_count, 0)

    def test_dispatch_names_the_stored_plan_run(self) -> None:
        worker, _, apply_port, _ = build_worker(work=approved_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.PLAN_COMPLETED
        )

        worker.handle(task)

        self.assertEqual(apply_port.dispatched_plan_run_ids, [PLAN_RUN_ID])

    def test_unapproved_work_does_not_dispatch(self) -> None:
        worker, store, apply_port, _ = build_worker(work=plan_work())
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=0, command=WorkflowCommand.PLAN_COMPLETED
        )
        with self.assertRaises(DeploymentWorkerError):
            worker.handle(task)
        self.assertEqual(store.receipts, [])
        self.assertEqual(apply_port.dispatch_count, 0)


class VerifyApplyTest(unittest.TestCase):
    def test_verifies_via_stored_run_reference_without_redispatch(self) -> None:
        # P1-1: APPLY_COMPLETED는 apply를 재dispatch하지 않고 저장된 run reference로 조회한다.
        worker, store, apply_port, actual_port = build_worker(
            work=approved_work(run_reference=run_reference()), run_facts=success_facts()
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        facts = worker.handle(task)
        self.assertIsInstance(facts, WorkflowRunFacts)
        self.assertIs(facts.conclusion, WorkflowConclusion.SUCCESS)
        self.assertEqual(apply_port.dispatch_count, 0)  # 재dispatch 없음
        self.assertEqual(len(store.verifications), 1)
        self.assertEqual(len(actual_port.reread_calls), 1)

    def test_missing_run_reference_is_error(self) -> None:
        worker, _, _, _ = build_worker(
            work=approved_work(run_reference=None), run_facts=success_facts()
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentWorkerError):
            worker.handle(task)

    def test_unregistered_run_is_blocked(self) -> None:
        worker, store, _, actual_port = build_worker(
            work=approved_work(run_reference=run_reference())
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)
        self.assertEqual(store.verifications, [])
        self.assertEqual(actual_port.reread_calls, [])

    def test_wrong_plan_hash_run_is_blocked(self) -> None:
        wrong = WorkflowRunFacts(
            run_id=RUN_ID,
            repository_id=REPOSITORY_ID,
            workflow_path=APPLY_WORKFLOW,
            ref=COMMIT,
            commit_sha=COMMIT,
            conclusion=WorkflowConclusion.SUCCESS,
            plan_hash="0" * 64,
        )
        worker, _, _, actual_port = build_worker(
            work=approved_work(run_reference=run_reference()), run_facts=wrong
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)
        self.assertEqual(actual_port.reread_calls, [])

    def test_non_success_conclusion_is_blocked(self) -> None:
        failed = WorkflowRunFacts(
            run_id=RUN_ID,
            repository_id=REPOSITORY_ID,
            workflow_path=APPLY_WORKFLOW,
            ref=COMMIT,
            commit_sha=COMMIT,
            conclusion=WorkflowConclusion.FAILURE,
            plan_hash=PLAN_HASH,
        )
        worker, _, _, _ = build_worker(
            work=approved_work(run_reference=run_reference()), run_facts=failed
        )
        task = WorkflowTask(
            job_id=JOB_ID, expected_revision=1, command=WorkflowCommand.APPLY_COMPLETED
        )
        with self.assertRaises(DeploymentApplyBlockedError):
            worker.handle(task)


if __name__ == "__main__":
    unittest.main()
