"""M3 D 실행 port의 결정적 in-memory Mock 어댑터.

`agent/runtime/deployment_ports.py`가 정의한 세 port를 구현한다. A/C가 Fixture/Mock으로
병렬 개발할 수 있도록, 실제 GitHub/Terraform/AWS 호출 없이 결정적으로 동작한다. 어떤 Mock도
실제 write/apply 표면을 노출하지 않는다.
"""

from __future__ import annotations

import hashlib

from agent.runtime.deployment_ports import (
    ActualRereadPort,
    ApplyDispatchPort,
    WorkflowRunReader,
)
from packages.contracts import (
    ApplyRunReference,
    AwsResourceSnapshot,
    DeploymentApproval,
    VerifiedRunOutcome,
)


class DeploymentPortError(RuntimeError):
    """M3 실행 port Mock 작업의 기본 실패 타입."""


class DeploymentPortScopeError(DeploymentPortError):
    """요청이 Mock에 설정된 단일 scope 밖을 대상으로 할 때 발생한다."""


def _derive_run_id(deployment_id: str, plan_hash: str) -> str:
    """approval 정체성으로부터 결정적 run_id를 만든다.

    같은 (deployment_id, plan_hash)는 항상 같은 run_id를 내므로, 같은 approval로 두 번
    dispatch돼도 같은 run을 가리킨다(idempotency).
    """
    seed = "\x1f".join((deployment_id, plan_hash))
    return "run-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class MockApplyDispatchPort(ApplyDispatchPort):
    """정확히 하나의 repository scope로 제한된 결정적 apply dispatch Mock.

    같은 approval로 재호출하면 새 run을 만들지 않고 같은 `ApplyRunReference`를 반환한다.
    """

    def __init__(self, *, repository_id: str) -> None:
        if not isinstance(repository_id, str) or not repository_id.strip():
            raise ValueError("repository_id must be a non-empty string")
        self._repository_id = repository_id
        # deployment_id -> 이미 dispatch된 run 참조. 재호출 idempotency의 근거.
        self._dispatched: dict[str, ApplyRunReference] = {}

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        state_lineage: str,
        state_serial: int,
        repository_id: str,
    ) -> ApplyRunReference:
        if not isinstance(approval, DeploymentApproval):
            raise TypeError("approval must be a DeploymentApproval")
        if not isinstance(state_lineage, str) or not state_lineage.strip():
            raise ValueError("state_lineage must be a non-empty string")
        if not isinstance(state_serial, int) or isinstance(state_serial, bool):
            raise TypeError("state_serial must be an int")
        if repository_id != self._repository_id:
            raise DeploymentPortScopeError("repository_id is outside the tool scope")

        existing = self._dispatched.get(approval.deployment_id)
        if existing is not None:
            # 같은 approval로 두 번째 호출 — 새 run을 만들지 않고 같은 참조를 돌려준다.
            return existing

        reference = ApplyRunReference(
            deployment_id=approval.deployment_id,
            repository_id=repository_id,
            run_id=_derive_run_id(approval.deployment_id, approval.plan_hash),
        )
        self._dispatched[approval.deployment_id] = reference
        return reference


class MockWorkflowRunReader(WorkflowRunReader):
    """(customer_id, repository_id) scope로 제한된 결정적 run 재조회 Mock.

    등록된 run은 그대로 반환하고, 미등록 run은 예외 대신 실패 결론(`not_found`)을 담은
    `VerifiedRunOutcome`을 반환한다 — 실패도 값이다(ADR-0017·0018).
    """

    def __init__(self, *, customer_id: str, repository_id: str) -> None:
        for name, value in (("customer_id", customer_id), ("repository_id", repository_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._customer_id = customer_id
        self._repository_id = repository_id
        self._runs: dict[str, VerifiedRunOutcome] = {}

    def register_run(self, outcome: VerifiedRunOutcome) -> None:
        """결정적 재조회를 위해 run outcome을 등록한다(테스트 seed)."""
        if not isinstance(outcome, VerifiedRunOutcome):
            raise TypeError("outcome must be a VerifiedRunOutcome")
        if outcome.repository_id != self._repository_id:
            raise DeploymentPortScopeError("outcome repository_id is outside the tool scope")
        if outcome.run_id in self._runs:
            raise ValueError(f"duplicate run_id: {outcome.run_id}")
        self._runs[outcome.run_id] = outcome

    def read_run(self, *, customer_id: str, repository_id: str, run_id: str) -> VerifiedRunOutcome:
        if customer_id != self._customer_id or repository_id != self._repository_id:
            raise DeploymentPortScopeError("customer_id/repository_id is outside the tool scope")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        existing = self._runs.get(run_id)
        if existing is not None:
            return existing
        # 미등록 run — 예외가 아니라 실패 결론을 값으로 반환한다. plan_hash는 대조에서
        # 반드시 어긋나도록 sentinel을 쓴다(D Worker가 승인 사실과 대조해 걸러낸다).
        return VerifiedRunOutcome(
            run_id=run_id,
            workflow_path="unknown",
            repository_id=repository_id,
            ref="unknown",
            conclusion="not_found",
            plan_hash="unknown",
        )


class MockActualRereadPort(ActualRereadPort):
    """(customer_id, aws_account_id) scope로 제한된 결정적 Actual 재조회 Mock.

    read-only이며 write 표면이 없다. 요청된 `resource_ids`로 좁혀 스냅샷을 반환하고,
    등록되지 않은 resource_id는 조용히 건너뛴다(재조회는 존재하는 Actual만 돌려준다).
    """

    def __init__(self, *, customer_id: str, aws_account_id: str) -> None:
        for name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        self._customer_id = customer_id
        self._aws_account_id = aws_account_id
        self._snapshots: dict[str, AwsResourceSnapshot] = {}

    def register_snapshot(self, snapshot: AwsResourceSnapshot) -> None:
        """재조회 대상 Actual 스냅샷을 등록한다(테스트 seed). scope를 강제한다."""
        if not isinstance(snapshot, AwsResourceSnapshot):
            raise TypeError("snapshot must be an AwsResourceSnapshot")
        if (
            snapshot.customer_id != self._customer_id
            or snapshot.aws_account_id != self._aws_account_id
        ):
            raise DeploymentPortScopeError("snapshot is outside the tool scope")
        if snapshot.resource_id in self._snapshots:
            raise ValueError(f"duplicate resource_id: {snapshot.resource_id}")
        self._snapshots[snapshot.resource_id] = snapshot

    def reread_actual(
        self, *, customer_id: str, aws_account_id: str, resource_ids: tuple[str, ...]
    ) -> tuple[AwsResourceSnapshot, ...]:
        if customer_id != self._customer_id or aws_account_id != self._aws_account_id:
            raise DeploymentPortScopeError("customer_id/aws_account_id is outside the tool scope")
        if not isinstance(resource_ids, tuple):
            raise TypeError("resource_ids must be a tuple")
        for resource_id in resource_ids:
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ValueError("resource_ids item must be a non-empty string")
        # 요청 순서를 보존해 결정적으로 반환하고, 등록되지 않은 것은 건너뛴다.
        return tuple(
            self._snapshots[resource_id]
            for resource_id in resource_ids
            if resource_id in self._snapshots
        )
