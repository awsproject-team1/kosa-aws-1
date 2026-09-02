"""D(Remediation/Deployment)가 소유하는 M3 실행 port 계약.

`docs/CONTRACTS.md`가 확정한 세 port의 시그니처를 그대로 정의한다. 세 port는 D가 소유하고
A/C가 주입받아 Fixture/Mock으로 병렬 구현한다(`Mockable` 의존성). 실제 GitHub Actions
dispatch, Terraform apply, AWS 재조회는 이 경계 밖(Integrated 단계)이며, 여기서는 주입
가능한 Protocol만 정의한다.

핵심 원칙:
- 세 port 모두 승인·정책 판정을 하지 않는다. 판정은 A(승인)와 B(정책)가 소유한다.
- `ApplyDispatchPort`는 같은 approval로 두 번 호출돼도 새 run을 만들지 않아야 한다.
  중복 방지의 정본은 `APPROVED → APPLYING` 조건부 전이다(ADR-0019 §5). port 구현은 그
  성질을 깨지 않도록 idempotent해야 한다.
- `WorkflowRunReader`는 EventBridge payload를 신뢰하지 않고 `run_id`로 Actions run을 다시
  읽는다(ADR-0019 §7). 재조회 실패도 값이므로 예외가 아니라 `VerifiedRunOutcome`으로 표현한다.
- `ActualRereadPort`는 새 표면이 아니라 M1 read-only AWS Resource Tool 재사용이다(ADR-0007).
  검증 단계에서 write 표면이 생기지 않는다.

`PlanRequestPort`는 D Worker 내부 호출이라 A/C가 주입받지 않으므로 이 모듈의 확정 대상이 아니다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from packages.contracts import (
    ApplyRunReference,
    AwsResourceSnapshot,
    DeploymentApproval,
    VerifiedRunOutcome,
)


@runtime_checkable
class ApplyDispatchPort(Protocol):
    """승인된 approval로 apply run을 dispatch한다. 이미 있는 run은 다시 만들지 않는다."""

    def dispatch_apply(
        self,
        *,
        approval: DeploymentApproval,
        state_lineage: str,
        state_serial: int,
        repository_id: str,
    ) -> ApplyRunReference:
        """approval에 바인딩된 apply run을 dispatch하고 그 참조를 반환한다.

        `state_lineage`·`state_serial`을 함께 받아 apply 직전 재검증을 이 경계 안에서
        끝낸다. `serial` 단독으로는 state 재생성을 잡지 못한다(ADR-0019 §2).
        """
        ...


@runtime_checkable
class WorkflowRunReader(Protocol):
    """run_id로 Actions run을 재조회해 권위 있는 완료 사실을 만든다.

    EventBridge payload를 신뢰하지 않는다(ADR-0019 §7).
    """

    def read_run(self, *, customer_id: str, repository_id: str, run_id: str) -> VerifiedRunOutcome:
        """`run_id`로 run을 재조회한 `VerifiedRunOutcome`을 반환한다(실패도 값)."""
        ...


@runtime_checkable
class ActualRereadPort(Protocol):
    """apply 후 AWS Actual을 다시 읽는다. 읽기 전용이며 write 표면이 없다."""

    def reread_actual(
        self, *, customer_id: str, aws_account_id: str, resource_ids: tuple[str, ...]
    ) -> tuple[AwsResourceSnapshot, ...]:
        """scope 안의 `resource_ids`로 좁혀 재조회한 Actual 스냅샷들을 반환한다."""
        ...
