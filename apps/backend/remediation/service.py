"""생성된 Terraform Patch를 승인된 판정·IaC Snapshot 하나에 묶어 제약한다.

ADR-0018: 조치 허가 판정(`RemediationPolicy.decide()`)은 A의 Remediation API가 앞에서
내리고, C의 `RemediationWorker`가 그 판정과 `RemediationContext`를 재조회해 이 service를
호출한다. `RemediationService.generate()`는 `decision.action`이 `TERRAFORM_PATCH`가 아니면
거부하여, 정책 게이트를 우회한 patch 생성을 타입 수준에서 막는다. generator(D 소유 port
구현체)는 이미 게이트를 통과한 `RemediationContext`만 받으므로 판정을 다시 알 필요가 없다.
D는 `RemediationPolicy`를 import하지 않고 판정 **값**에만 의존해 B 구현과 분리된 채 남는다.
"""

from typing import Protocol

from packages.contracts import (
    RemediationAction,
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
)


class RemediationContractError(ValueError):
    """생성된 patch가 판정·snapshot 경계에 묶이지 않았을 때 발생한다."""


class RemediationNotAutomatableError(ValueError):
    """정책 context가 사람 검토를 요구하여 patch 생성에 진입해서는 안 될 때 발생한다."""


class PatchGenerator(Protocol):
    def generate(self, *, context: RemediationContext) -> RemediationPatch: ...


class RemediationService:
    """GitHub 연동이 patch를 소비하기 전에, 주입된 generator의 출력을 검증한다."""

    def __init__(self, generator: PatchGenerator) -> None:
        if generator is None:
            raise TypeError("generator is required")
        self._generator = generator

    def generate(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationPatch:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        finding = context.finding
        finding_id = finding.finding_id
        snapshot = context.snapshot
        # 판정과 context가 같은 finding을 가리키는지, 그리고 patch 생성이 허가됐는지(게이트)
        # 확인한다. TERRAFORM_PATCH가 아닌 판정으로 여기까지 온 것은 orchestrator의 버그다.
        if (
            decision.finding_id,
            decision.resource_id,
            decision.rule_id,
            decision.rule_version,
            decision.perspective,
        ) != (
            finding.finding_id,
            finding.resource_id,
            finding.rule_id,
            finding.rule_version,
            finding.perspective,
        ):
            raise RemediationContractError("remediation decision is outside context")
        if decision.action is not RemediationAction.TERRAFORM_PATCH:
            raise RemediationContractError("remediation decision does not permit a Terraform patch")
        patch = self._generator.generate(context=context)
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
