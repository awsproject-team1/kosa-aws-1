"""D-owned, revision-bound deployment execution worker (ADR-0019).

The worker consumes the three deployment `WorkflowCommand`s and drives exactly one
injected D port per command. It never authorizes a deployment and never decides policy —
approval is an A-owned fact reloaded here and cross-checked, not produced. Every value the
ports return is re-validated against the reloaded approval before the flow advances.

Command -> port (ADR-0019, canonical ports in apps/backend/deployment/ports.py):
- RUN_DEPLOYMENT  -> PlanRequestPort.request_plan     (refreshed plan, section 1/2/3)
- PLAN_COMPLETED  -> ApplyDispatchPort.dispatch_apply (workflow_dispatch only, section 5)
- APPLY_COMPLETED -> WorkflowRunReader.read_run then   (authoritative run facts, section 7)
                     ActualRereadPort.reread_actual    (post-apply Actual reread, ADR-0020)

Two facts are never trusted as state:
- the queue payload (WorkflowTask) - durable work is reloaded by (job_id, revision)
  (ADR-0013, mirrors the C Remediation Worker).
- the completion Event - the run is re-read by the stored WorkflowRunReference (real GitHub
  run_id) and its plan_hash/repository_id/ref/workflow_path are matched against the approved
  plan (ADR-0019 section 7). The worker never re-dispatches apply to obtain a run id.
"""

from dataclasses import dataclass
from typing import Protocol

from apps.backend.deployment.ports import (
    ActualRereadPort,
    ApplyDispatchPort,
    PlanRequestPort,
    WorkflowRunReader,
)
from packages.contracts import (
    ApplyDispatchReceipt,
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

# apply run을 성공으로 인정할 때 workflow path가 반드시 이 allow-list 안이어야 한다
# (ADR-0019 section 7). 고객이 자기 repo에 설치하는 apply workflow의 정본 경로다.
# live 어댑터도 같은 경로를 쓰지만, 순환 import를 피하려고 worker가 정본으로 둔다.
APPLY_WORKFLOW_PATHS = frozenset(
    {".github/workflows/terraform-apply.yml", ".github/workflows/terraform-apply.yaml"}
)
_APPLY_WORKFLOW_PATHS = APPLY_WORKFLOW_PATHS


class DeploymentWorkerError(ValueError):
    """Raised when a task cannot safely enter a deployment port."""


class DeploymentWorkNotFoundError(DeploymentWorkerError):
    """The authoritative work item is absent or not at the expected revision."""


class DeploymentApplyBlockedError(DeploymentWorkerError):
    """A verified run does not match the approved facts; the flow must not advance.

    ADR-0019 section 7: 하나라도 다르면 재시도하지 않고 MANUAL_REVIEW로 보낸다. 이 예외는
    호출자(A 상태 전이)가 MANUAL_REVIEW로 옮길 신호이며, 자동 재시도의 신호가 아니다.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentWork:
    """Authoritative A-owned deployment work reloaded by D for one exact revision.

    D는 이 값을 만들지 않고 다시 읽어 검증만 한다. `approval`은 A가 이미 record한 사실이며,
    `RUN_DEPLOYMENT` 단계(plan 생성 전)에는 아직 없으므로 optional이다. `run_reference`는
    apply dispatch 후 EventBridge가 실어 온 실제 GitHub `run_id`로 A가 durable하게 기록하며,
    `APPLY_COMPLETED` 단계에서 D가 그것으로 run을 재조회한다(§7). `sync_target`은 apply 후
    Actual 재조회 대상 commit이다(ADR-0020).
    """

    customer_id: str
    deployment_id: str
    repository_id: str
    aws_account_id: str
    job_id: str
    revision: int
    commit_sha: str
    plan: TerraformPlan | None = None
    state_version: TerraformStateVersion | None = None
    approval: DeploymentApproval | None = None
    run_reference: WorkflowRunReference | None = None
    sync_target: RemediationSyncTarget | None = None

    def __post_init__(self) -> None:
        for name in (
            "customer_id",
            "deployment_id",
            "repository_id",
            "aws_account_id",
            "job_id",
            "commit_sha",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if self.plan is not None:
            if not isinstance(self.plan, TerraformPlan):
                raise TypeError("plan must be a TerraformPlan or None")
            if self.plan.deployment_id != self.deployment_id:
                raise ValueError("plan deployment_id does not match work")
            if self.plan.commit_sha != self.commit_sha:
                raise ValueError("plan commit_sha does not match work")
            if self.plan.artifact.customer_id != self.customer_id:
                raise ValueError("plan customer scope does not match work")
        if self.state_version is not None and not isinstance(
            self.state_version, TerraformStateVersion
        ):
            raise TypeError("state_version must be a TerraformStateVersion or None")
        if self.approval is not None:
            if not isinstance(self.approval, DeploymentApproval):
                raise TypeError("approval must be a DeploymentApproval or None")
            if self.approval.deployment_id != self.deployment_id:
                raise ValueError("approval deployment_id does not match work")
        if self.run_reference is not None:
            if not isinstance(self.run_reference, WorkflowRunReference):
                raise TypeError("run_reference must be a WorkflowRunReference or None")
            if (
                self.run_reference.deployment_id != self.deployment_id
                or self.run_reference.repository_id != self.repository_id
            ):
                raise ValueError("run_reference does not match work")
        if self.sync_target is not None:
            if not isinstance(self.sync_target, RemediationSyncTarget):
                raise TypeError("sync_target must be a RemediationSyncTarget or None")
            if self.sync_target.customer_id != self.customer_id:
                raise ValueError("sync_target customer scope does not match work")


class DeploymentWorkRepository(Protocol):
    def get_work(self, *, job_id: str, expected_revision: int) -> DeploymentWork | None: ...


class DeploymentPlanStore(Protocol):
    """Persist a refreshed plan execution result once; retries at the same revision absorb."""

    def put_plan_if_absent(self, *, work: DeploymentWork, result: PlanExecutionResult) -> None: ...


class DeploymentRunStore(Protocol):
    """Persist a dispatched apply run receipt once; idempotent per deployment."""

    def put_receipt_if_absent(
        self, *, work: DeploymentWork, receipt: ApplyDispatchReceipt
    ) -> None: ...


class DeploymentVerificationStore(Protocol):
    """Persist the authoritative verified run facts once."""

    def put_verification_if_absent(
        self, *, work: DeploymentWork, facts: WorkflowRunFacts
    ) -> None: ...


class DeploymentWorker:
    """Drive one stored deployment through exactly one injected D port per command."""

    def __init__(
        self,
        *,
        work_repository: DeploymentWorkRepository,
        plan_port: PlanRequestPort,
        apply_port: ApplyDispatchPort,
        run_reader: WorkflowRunReader,
        actual_port: ActualRereadPort,
        plan_store: DeploymentPlanStore,
        run_store: DeploymentRunStore,
        verification_store: DeploymentVerificationStore,
    ) -> None:
        dependencies = (
            work_repository,
            plan_port,
            apply_port,
            run_reader,
            actual_port,
            plan_store,
            run_store,
            verification_store,
        )
        if any(dependency is None for dependency in dependencies):
            raise TypeError("all deployment worker dependencies are required")
        self._work_repository = work_repository
        self._plan_port = plan_port
        self._apply_port = apply_port
        self._run_reader = run_reader
        self._actual_port = actual_port
        self._plan_store = plan_store
        self._run_store = run_store
        self._verification_store = verification_store

    def handle(
        self, task: WorkflowTask
    ) -> PlanExecutionResult | ApplyDispatchReceipt | WorkflowRunFacts:
        if not isinstance(task, WorkflowTask):
            raise TypeError("task must be a WorkflowTask")
        work = self._reload_work(task)
        if task.command is WorkflowCommand.RUN_DEPLOYMENT:
            return self._run_plan(work)
        if task.command is WorkflowCommand.PLAN_COMPLETED:
            return self._dispatch_apply(work)
        if task.command is WorkflowCommand.APPLY_COMPLETED:
            return self._verify_apply(work)
        raise DeploymentWorkerError("unsupported deployment command")

    def _reload_work(self, task: WorkflowTask) -> DeploymentWork:
        work = self._work_repository.get_work(
            job_id=task.job_id, expected_revision=task.expected_revision
        )
        if work is None:
            raise DeploymentWorkNotFoundError(
                "deployment work is missing or stale for the expected revision"
            )
        if not isinstance(work, DeploymentWork):
            raise DeploymentWorkerError("repository returned invalid deployment work")
        if work.job_id != task.job_id or work.revision != task.expected_revision:
            raise DeploymentWorkerError("deployment work does not match the task")
        return work

    def _run_plan(self, work: DeploymentWork) -> PlanExecutionResult:
        result = self._plan_port.request_plan(
            customer_id=work.customer_id,
            deployment_id=work.deployment_id,
            repository_id=work.repository_id,
            commit_sha=work.commit_sha,
        )
        if not isinstance(result, PlanExecutionResult):
            raise DeploymentWorkerError("plan port must return a PlanExecutionResult")
        plan = result.plan
        if plan.deployment_id != work.deployment_id or plan.commit_sha != work.commit_sha:
            raise DeploymentWorkerError("plan is outside the deployment work")
        if plan.artifact.customer_id != work.customer_id:
            raise DeploymentWorkerError("plan is outside the customer scope")
        if plan.artifact.repository_id not in (None, work.repository_id):
            raise DeploymentWorkerError("plan is outside the repository scope")
        # PlanExecutionResult already binds the binary to the plan artifact, so
        # confirming the binary against the same work scope leaves no gap where a
        # foreign binary could reach apply.
        if result.binary_artifact.customer_id != work.customer_id:
            raise DeploymentWorkerError("plan binary is outside the customer scope")
        if result.binary_artifact.repository_id not in (None, work.repository_id):
            raise DeploymentWorkerError("plan binary is outside the repository scope")
        self._plan_store.put_plan_if_absent(work=work, result=result)
        return result

    def _dispatch_apply(self, work: DeploymentWork) -> ApplyDispatchReceipt:
        approval, plan, state_version = self._require_approved_plan(work)
        receipt = self._apply_port.dispatch_apply(
            approval=approval, plan=plan, state_version=state_version
        )
        if not isinstance(receipt, ApplyDispatchReceipt):
            raise DeploymentWorkerError("apply port must return an ApplyDispatchReceipt")
        if (
            receipt.deployment_id != work.deployment_id
            or receipt.repository_id != work.repository_id
        ):
            raise DeploymentWorkerError("apply dispatch receipt is outside the deployment work")
        if receipt.workflow_path not in _APPLY_WORKFLOW_PATHS:
            raise DeploymentWorkerError("apply dispatch receipt workflow path is not allow-listed")
        self._run_store.put_receipt_if_absent(work=work, receipt=receipt)
        return receipt

    def _verify_apply(self, work: DeploymentWork) -> WorkflowRunFacts:
        approval, _plan, _state = self._require_approved_plan(work)
        if work.run_reference is None:
            # 재조회할 run 좌표가 없으면 진행하지 않는다. run_id는 A가 완료 Event에서 durable하게
            # 기록한 실제 GitHub run id다 — 여기서 새 apply를 dispatch해 만들지 않는다(§5·§7).
            raise DeploymentWorkerError("deployment work has no run reference to verify")
        if work.sync_target is None:
            raise DeploymentWorkerError("deployment work has no sync target for reread")
        facts = self._run_reader.read_run(work.run_reference)
        if not isinstance(facts, WorkflowRunFacts):
            raise DeploymentWorkerError("run reader must return WorkflowRunFacts")
        self._require_run_matches_approval(work, approval, facts)
        # 승인 사실과 완전히 일치하고 성공한 run만 apply 후 Actual을 재조회한다(ADR-0020).
        self._actual_port.reread_actual(
            customer_id=work.customer_id,
            deployment_id=work.deployment_id,
            sync_target=work.sync_target,
        )
        self._verification_store.put_verification_if_absent(work=work, facts=facts)
        return facts

    @staticmethod
    def _require_approved_plan(
        work: DeploymentWork,
    ) -> tuple[DeploymentApproval, TerraformPlan, TerraformStateVersion]:
        """apply 계열 command는 stored plan/approval/state가 정확히 바인딩됐을 때만 진행한다."""
        if work.plan is None or work.approval is None or work.state_version is None:
            raise DeploymentWorkerError("deployment work is not approved for apply")
        plan = work.plan
        if plan.plan_hash != plan.artifact.content_sha256:
            raise DeploymentWorkerError("stored plan digest is not exact")
        if not work.approval.matches(plan):
            raise DeploymentWorkerError("approval is not bound to the stored plan")
        return work.approval, plan, work.state_version

    def _require_run_matches_approval(
        self, work: DeploymentWork, approval: DeploymentApproval, facts: WorkflowRunFacts
    ) -> None:
        """재조회한 run이 승인 사실과 하나라도 다르면 apply를 진행하지 않는다(ADR-0019 section 7)."""
        if facts.repository_id != work.repository_id:
            raise DeploymentApplyBlockedError("verified run repository is not the approved one")
        if facts.workflow_path not in _APPLY_WORKFLOW_PATHS:
            raise DeploymentApplyBlockedError("verified run workflow path is not allow-listed")
        if facts.ref != approval.commit_sha or facts.commit_sha != approval.commit_sha:
            raise DeploymentApplyBlockedError("verified run commit is not the approved commit")
        if facts.plan_hash != approval.plan_hash:
            raise DeploymentApplyBlockedError("verified run plan_hash is not the approved plan")
        if facts.conclusion is not WorkflowConclusion.SUCCESS:
            raise DeploymentApplyBlockedError("verified run did not conclude successfully")
