"""M3 D 실행 port의 결정적 in-memory Mock 어댑터.

`apps/backend/deployment/ports.py`가 정의한 네 정본 port를 구현한다. A/C가 Fixture/Mock으로
병렬 개발할 수 있도록, 실제 GitHub/Terraform/AWS 호출 없이 결정적으로 동작한다. 어떤 Mock도
실제 write/apply 표면을 노출하지 않는다.

정본 반환형을 그대로 쓴다: `PlanExecutionResult`, `ApplyDispatchReceipt`, `WorkflowRunFacts`,
`WorkflowRunReference`, `WorkflowConclusion` (`packages/contracts`).
"""

from __future__ import annotations

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
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
)
from packages.contracts.remediation import RemediationSyncTarget

# apply를 트리거하는 workflow 파일 경로(고객 repo 설치 경로). dispatch receipt가 이 값을 담는다.
_APPLY_WORKFLOW_PATH = ".github/workflows/terraform-apply.yml"


class DeploymentPortError(RuntimeError):
    """M3 실행 port Mock 작업의 기본 실패 타입."""


class DeploymentPortScopeError(DeploymentPortError):
    """요청이 Mock에 설정된 단일 scope 밖을 대상으로 할 때 발생한다."""


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class MockPlanRequestPort(PlanRequestPort):
    """(customer_id, repository_id) scope로 제한된 결정적 plan 요청 Mock.

    등록된 (deployment_id, commit_sha)에 대해 미리 seed한 `PlanExecutionResult`를 그대로
    반환한다. 실제 Terraform 실행 없이 D Worker 흐름을 병렬 개발할 수 있게 한다.
    """

    def __init__(self, *, customer_id: str, repository_id: str) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._repository_id = _require_non_empty(repository_id, "repository_id")
        self._plans: dict[tuple[str, str], PlanExecutionResult] = {}

    def register_plan(
        self, *, deployment_id: str, commit_sha: str, result: PlanExecutionResult
    ) -> None:
        """결정적 반환을 위해 plan 실행 결과를 등록한다(테스트 seed)."""
        if not isinstance(result, PlanExecutionResult):
            raise TypeError("result must be a PlanExecutionResult")
        if result.plan.deployment_id != deployment_id:
            raise ValueError("result plan deployment_id must match deployment_id")
        if result.plan.commit_sha != commit_sha:
            raise ValueError("result plan commit_sha must match commit_sha")
        key = (deployment_id, commit_sha)
        if key in self._plans:
            raise ValueError(f"duplicate plan request: {key}")
        self._plans[key] = result

    def request_plan(
        self, *, customer_id: str, deployment_id: str, repository_id: str, commit_sha: str
    ) -> PlanExecutionResult:
        if customer_id != self._customer_id or repository_id != self._repository_id:
            raise DeploymentPortScopeError("customer_id/repository_id is outside the tool scope")
        _require_non_empty(deployment_id, "deployment_id")
        _require_non_empty(commit_sha, "commit_sha")
        result = self._plans.get((deployment_id, commit_sha))
        if result is None:
            # plan 부재는 흐름이 성립하지 않는 오류다(재조회 실패와 다르다).
            raise DeploymentPortError(
                f"no plan registered for deployment {deployment_id} at {commit_sha}"
            )
        return result


class MockApplyDispatchPort(ApplyDispatchPort):
    """정확히 하나의 repository scope로 제한된 결정적 apply dispatch Mock.

    dispatch는 `workflow_dispatch` 확인(receipt)만 돌려준다. receipt에는 run_id가 없다 —
    권위 있는 apply 사실은 `WorkflowRunReader`로 run을 재조회해 얻는다(ADR-0019 §5·§7).
    """

    def __init__(self, *, customer_id: str, repository_id: str) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._repository_id = _require_non_empty(repository_id, "repository_id")
        # dispatch 횟수를 기록해 idempotency 테스트가 관측할 수 있게 한다.
        self.dispatch_count = 0
        self.dispatched_plan_run_ids: list[str] = []

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        plan: TerraformPlan,
        state_version: TerraformStateVersion,
        plan_run: WorkflowRunReference,
    ) -> ApplyDispatchReceipt:
        if not isinstance(approval, DeploymentApproval):
            raise TypeError("approval must be a DeploymentApproval")
        if not isinstance(plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(state_version, TerraformStateVersion):
            raise TypeError("state_version must be a TerraformStateVersion")
        if not isinstance(plan_run, WorkflowRunReference):
            raise TypeError("plan_run must be a WorkflowRunReference")
        if not approval.matches(plan):
            raise DeploymentPortError("approval is not bound to the plan")
        if plan.artifact.repository_id not in (None, self._repository_id):
            raise DeploymentPortScopeError("plan is outside the tool scope")
        if plan_run.deployment_id != approval.deployment_id:
            raise DeploymentPortError("plan run is not bound to the approved deployment")
        if plan_run.repository_id != self._repository_id:
            raise DeploymentPortScopeError("plan run is outside the tool scope")
        # live 어댑터가 보내는 `plan_run_id`에 해당한다. 관측 가능하게 남겨 테스트가 apply가
        # 어느 plan run의 artifact를 지목했는지 확인할 수 있게 한다.
        self.dispatched_plan_run_ids.append(plan_run.run_id)
        self.dispatch_count += 1
        return ApplyDispatchReceipt(
            deployment_id=approval.deployment_id,
            repository_id=self._repository_id,
            workflow_path=_APPLY_WORKFLOW_PATH,
        )


class MockWorkflowRunReader(WorkflowRunReader):
    """(customer_id, repository_id) scope로 제한된 결정적 run 재조회 Mock.

    등록된 run은 그대로 반환하고, 미등록 run은 예외 대신 실패 결론(`FAILURE`)에 승인 대조를
    반드시 어긋나게 하는 sentinel을 담은 `WorkflowRunFacts`를 반환한다 — 실패도 값이다(§7).
    """

    def __init__(self, *, customer_id: str, repository_id: str) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._repository_id = _require_non_empty(repository_id, "repository_id")
        self._runs: dict[str, WorkflowRunFacts] = {}

    def register_run(self, facts: WorkflowRunFacts) -> None:
        """결정적 재조회를 위해 run facts를 등록한다(테스트 seed). scope를 강제한다."""
        if not isinstance(facts, WorkflowRunFacts):
            raise TypeError("facts must be a WorkflowRunFacts")
        if facts.repository_id != self._repository_id:
            raise DeploymentPortScopeError("facts repository_id is outside the tool scope")
        if facts.run_id in self._runs:
            raise ValueError(f"duplicate run_id: {facts.run_id}")
        self._runs[facts.run_id] = facts

    def read_run(self, reference: WorkflowRunReference) -> WorkflowRunFacts:
        if not isinstance(reference, WorkflowRunReference):
            raise TypeError("reference must be a WorkflowRunReference")
        if reference.repository_id != self._repository_id:
            raise DeploymentPortScopeError("reference repository_id is outside the tool scope")
        existing = self._runs.get(reference.run_id)
        if existing is not None:
            return existing
        # 미등록 run — 예외가 아니라 실패 결론 값. sentinel로 승인 대조에서 반드시 걸린다.
        return WorkflowRunFacts(
            run_id=reference.run_id,
            repository_id=reference.repository_id,
            workflow_path="unknown",
            ref="unknown",
            commit_sha="unknown",
            conclusion=WorkflowConclusion.FAILURE,
            plan_hash="unknown",
        )


class MockActualRereadPort(ActualRereadPort):
    """(customer_id) scope로 제한된 결정적 Actual 재조회 Mock.

    정본 port는 반환값이 없다(`None`) — apply 후 Actual은 검증 Assessment가 다시 평가한다
    (ADR-0020). 이 Mock은 호출 사실만 기록해 D Worker 흐름 테스트가 관측할 수 있게 한다.
    """

    def __init__(self, *, customer_id: str) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self.reread_calls: list[tuple[str, RemediationSyncTarget]] = []

    def reread_actual(
        self, *, customer_id: str, deployment_id: str, sync_target: RemediationSyncTarget
    ) -> None:
        if customer_id != self._customer_id:
            raise DeploymentPortScopeError("customer_id is outside the tool scope")
        _require_non_empty(deployment_id, "deployment_id")
        if not isinstance(sync_target, RemediationSyncTarget):
            raise TypeError("sync_target must be a RemediationSyncTarget")
        if sync_target.customer_id != self._customer_id:
            raise DeploymentPortScopeError("sync_target is outside the tool scope")
        self.reread_calls.append((deployment_id, sync_target))
