"""D-owned, revision-bound deployment execution worker (ADR-0019).

The worker consumes the three deployment `WorkflowCommand`s and drives exactly one
injected D port per command. It never authorizes a deployment and never decides policy —
approval is an A-owned fact reloaded here and cross-checked, not produced. Every value the
ports return is re-validated against the reloaded approval before the flow advances.

Command → port (ADR-0019):
- `RUN_DEPLOYMENT`  → `PlanRequestPort.request_plan`      (refreshed plan, section 1/2/3)
- `PLAN_COMPLETED`  → `ApplyDispatchPort.dispatch_apply`  (idempotent apply run, section 5)
- `APPLY_COMPLETED` → `WorkflowRunReader.read_run` then    (authoritative run fact, section 7)
                       `ActualRereadPort.reread_actual`    (post-apply Actual reread, ADR-0020)

Two facts are never trusted as state and are always re-derived here:
- the queue payload (`WorkflowTask`) — durable work is reloaded by `(job_id, revision)`
  (ADR-0013, mirrors the C Remediation Worker).
- the completion Event — the run is re-read by `run_id` and its `plan_hash`/`repository_id`/
  `ref`/`workflow_path` are matched against the approved plan (ADR-0019 section 7).
"""

from dataclasses import dataclass
from typing import Protocol

from agent.runtime.deployment_ports import (
    ActualRereadPort,
    ApplyDispatchPort,
    PlanRequestPort,
    WorkflowRunReader,
)
from agent.runtime.live_deployment_ports import APPLY_WORKFLOW_PATHS
from packages.contracts import (
    ApplyRunReference,
    AwsResourceSnapshot,
    DeploymentApproval,
    PlanRequestOutcome,
    TerraformPlan,
    TerraformStateVersion,
    VerifiedRunOutcome,
    WorkflowCommand,
    WorkflowTask,
)

# apply run을 성공으로 인정할 때 workflow path가 반드시 이 allow-list 안이어야 한다
# (ADR-0019 section 7). 정본은 live 어댑터가 노출하는 `APPLY_WORKFLOW_PATHS` 하나이며,
# worker와 어댑터가 다른 경로를 대조하지 않도록 같은 상수를 재사용한다.
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
    `RUN_DEPLOYMENT` 단계(plan 생성 전)에는 아직 없으므로 optional이다.
    """

    customer_id: str
    deployment_id: str
    repository_id: str
    aws_account_id: str
    job_id: str
    revision: int
    commit_sha: str
    mapped_resource_ids: tuple[str, ...] = ()
    plan: TerraformPlan | None = None
    state_version: TerraformStateVersion | None = None
    approval: DeploymentApproval | None = None

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
        if not isinstance(self.mapped_resource_ids, tuple):
            raise TypeError("mapped_resource_ids must be a tuple")
        for resource_id in self.mapped_resource_ids:
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ValueError("mapped_resource_ids item must be a non-empty string")
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


class DeploymentWorkRepository(Protocol):
    def get_work(self, *, job_id: str, expected_revision: int) -> DeploymentWork | None: ...


class DeploymentPlanStore(Protocol):
    """Persist a refreshed plan outcome once; retries at the same revision are absorbed."""

    def put_plan_if_absent(self, *, work: DeploymentWork, outcome: PlanRequestOutcome) -> None: ...


class ApplyRunStore(Protocol):
    """Persist a dispatched apply run reference once; idempotent per deployment."""

    def put_run_if_absent(self, *, work: DeploymentWork, reference: ApplyRunReference) -> None: ...


class VerifiedActualStore(Protocol):
    """Persist the authoritative run outcome and post-apply Actual once."""

    def put_verification_if_absent(
        self,
        *,
        work: DeploymentWork,
        outcome: VerifiedRunOutcome,
        actual: tuple[AwsResourceSnapshot, ...],
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
        run_store: ApplyRunStore,
        verification_store: VerifiedActualStore,
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
    ) -> PlanRequestOutcome | ApplyRunReference | VerifiedRunOutcome:
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

    def _run_plan(self, work: DeploymentWork) -> PlanRequestOutcome:
        outcome = self._plan_port.request_plan(
            customer_id=work.customer_id,
            deployment_id=work.deployment_id,
            repository_id=work.repository_id,
            commit_sha=work.commit_sha,
        )
        if not isinstance(outcome, PlanRequestOutcome):
            raise DeploymentWorkerError("plan port must return a PlanRequestOutcome")
        plan = outcome.plan
        if plan.deployment_id != work.deployment_id or plan.commit_sha != work.commit_sha:
            raise DeploymentWorkerError("plan is outside the deployment work")
        if plan.artifact.customer_id != work.customer_id:
            raise DeploymentWorkerError("plan is outside the customer scope")
        if plan.artifact.repository_id not in (None, work.repository_id):
            raise DeploymentWorkerError("plan is outside the repository scope")
        self._plan_store.put_plan_if_absent(work=work, outcome=outcome)
        return outcome

    def _dispatch_apply(self, work: DeploymentWork) -> ApplyRunReference:
        approval = self._require_approved_plan(work)
        assert work.state_version is not None  # _require_approved_plan guarantees it
        reference = self._apply_port.dispatch_apply(
            approval=approval,
            state_lineage=work.state_version.lineage,
            state_serial=work.state_version.serial,
            repository_id=work.repository_id,
        )
        if not isinstance(reference, ApplyRunReference):
            raise DeploymentWorkerError("apply port must return an ApplyRunReference")
        if (
            reference.deployment_id != work.deployment_id
            or reference.repository_id != work.repository_id
        ):
            raise DeploymentWorkerError("apply run reference is outside the deployment work")
        self._run_store.put_run_if_absent(work=work, reference=reference)
        return reference

    def _verify_apply(self, work: DeploymentWork) -> VerifiedRunOutcome:
        approval = self._require_approved_plan(work)
        assert work.state_version is not None  # _require_approved_plan guarantees it
        # dispatch는 idempotent하므로 재호출해도 새 run을 만들지 않고 같은 run_id를 돌려준다
        # (ADR-0019 section 5). 이 값으로 run을 재조회한다.
        reference = self._apply_port.dispatch_apply(
            approval=approval,
            state_lineage=work.state_version.lineage,
            state_serial=work.state_version.serial,
            repository_id=work.repository_id,
        )
        outcome = self._run_reader.read_run(
            customer_id=work.customer_id,
            repository_id=work.repository_id,
            run_id=reference.run_id,
        )
        if not isinstance(outcome, VerifiedRunOutcome):
            raise DeploymentWorkerError("run reader must return a VerifiedRunOutcome")
        self._require_run_matches_approval(work, approval, outcome)
        # 승인 사실과 완전히 일치하고 성공한 run만 apply 후 Actual을 재조회한다(ADR-0020).
        actual = self._actual_port.reread_actual(
            customer_id=work.customer_id,
            aws_account_id=work.aws_account_id,
            resource_ids=self._mapped_resource_ids(work),
        )
        if not isinstance(actual, tuple):
            raise DeploymentWorkerError("actual port must return a tuple")
        for snapshot in actual:
            if not isinstance(snapshot, AwsResourceSnapshot):
                raise DeploymentWorkerError("actual reread returned a non-snapshot value")
            if (
                snapshot.customer_id != work.customer_id
                or snapshot.aws_account_id != work.aws_account_id
            ):
                raise DeploymentWorkerError("actual snapshot is outside the deployment scope")
        self._verification_store.put_verification_if_absent(
            work=work, outcome=outcome, actual=actual
        )
        return outcome

    @staticmethod
    def _require_approved_plan(work: DeploymentWork) -> DeploymentApproval:
        """apply 계열 command는 stored plan/approval/state가 정확히 바인딩됐을 때만 진행한다."""
        if work.plan is None or work.approval is None or work.state_version is None:
            raise DeploymentWorkerError("deployment work is not approved for apply")
        plan = work.plan
        if plan.plan_hash != plan.artifact.content_sha256:
            raise DeploymentWorkerError("stored plan digest is not exact")
        if not work.approval.matches(plan):
            raise DeploymentWorkerError("approval is not bound to the stored plan")
        return work.approval

    def _require_run_matches_approval(
        self, work: DeploymentWork, approval: DeploymentApproval, outcome: VerifiedRunOutcome
    ) -> None:
        """재조회한 run이 승인 사실과 하나라도 다르면 apply를 진행하지 않는다(ADR-0019 section 7)."""
        if outcome.repository_id != work.repository_id:
            raise DeploymentApplyBlockedError("verified run repository is not the approved one")
        if outcome.workflow_path not in _APPLY_WORKFLOW_PATHS:
            raise DeploymentApplyBlockedError("verified run workflow path is not allow-listed")
        if outcome.ref != approval.commit_sha:
            raise DeploymentApplyBlockedError("verified run ref is not the approved commit")
        if outcome.plan_hash != approval.plan_hash:
            raise DeploymentApplyBlockedError("verified run plan_hash is not the approved plan")
        if not outcome.succeeded:
            raise DeploymentApplyBlockedError("verified run did not conclude successfully")

    @staticmethod
    def _mapped_resource_ids(work: DeploymentWork) -> tuple[str, ...]:
        """재조회 대상을 planned 집합(readiness에 매핑된 리소스)으로 좁힌다(ADR-0020 section 8).

        전체 재평가가 기본이어도 읽기 대상은 planned 집합에서 나온다. work의
        `mapped_resource_ids`가 그 durable한 근거이며, D는 여기서 read-only scope만 강제한다.
        """
        return work.mapped_resource_ids
