"""생성된 Terraform Patch를 승인된 판정·IaC Snapshot 하나에 묶어 제약한다.

ADR-0018: 조치 허가 판정(`RemediationPolicy.decide()`)은 A의 Remediation API가 앞에서
내리고, 그 결과인 `RemediationDecision`을 D가 소비한다. `RemediationService.generate()`는
판정을 **인자로 요구**하고, `TERRAFORM_PATCH`가 아닌 판정으로 호출되면 거부한다. 이렇게
"판정 없이는 patch를 만들 수 없다"를 타입 수준에서 강제하므로, A를 우회하는 경로(재시도,
배치, worker 직접 호출)도 게이트를 벗어날 수 없다. D는 `RemediationPolicy`를 import하지
않고 판정 **값**에만 의존하므로 B 구현과 분리된 채로 남는다.
"""

from typing import Protocol

from packages.contracts import (
    IaCSnapshot,
    RemediationAction,
    RemediationDecision,
    RemediationPatch,
)


class RemediationContractError(ValueError):
    """생성된 patch가 판정·snapshot 경계에 묶이지 않았을 때 발생한다."""


class PatchGenerator(Protocol):
    def generate(
        self, *, decision: RemediationDecision, snapshot: IaCSnapshot
    ) -> RemediationPatch: ...


class RemediationService:
    """GitHub 연동이 patch를 소비하기 전에, 주입된 generator의 출력을 검증한다."""

    def __init__(self, generator: PatchGenerator) -> None:
        if generator is None:
            raise TypeError("generator is required")
        self._generator = generator

    def generate(self, *, decision: RemediationDecision, snapshot: IaCSnapshot) -> RemediationPatch:
        # 판정 게이트(ADR-0018 D3): patch 생성은 TERRAFORM_PATCH 판정에서만 허용된다.
        # MANUAL_REVIEW/SUPPRESSED/ACTUAL_SYNC 판정은 고객에게 보여줄 정상 "값"이지만,
        # 그 판정을 들고 generate()까지 온 것은 orchestrator의 버그이므로 예외로 거부한다.
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        if decision.action is not RemediationAction.TERRAFORM_PATCH:
            raise RemediationContractError(
                f"generate called with a non-patch decision: {decision.action}"
            )
        if not isinstance(snapshot, IaCSnapshot):
            raise TypeError("snapshot must be an IaCSnapshot")

        # finding_id는 판정에서 꺼낸다. 별도 인자로 받으면 "판정은 finding-A, 대상은
        # finding-B"인 어긋난 호출이 다시 타입을 통과하게 된다(ADR-0018 D3).
        finding_id = decision.finding_id

        patch = self._generator.generate(decision=decision, snapshot=snapshot)
        if not isinstance(patch, RemediationPatch):
            raise RemediationContractError("generator must return a RemediationPatch")
        if patch.finding_id != finding_id:
            raise RemediationContractError("patch finding_id is outside request context")
        if patch.base_commit_sha != snapshot.commit_sha:
            raise RemediationContractError("patch is not bound to the IaC snapshot commit")
        if patch.artifact.customer_id != snapshot.customer_id:
            raise RemediationContractError("patch artifact customer scope does not match snapshot")
        if patch.artifact.repository_id != snapshot.repository_id:
            raise RemediationContractError(
                "patch artifact repository scope does not match snapshot"
            )
        return patch
