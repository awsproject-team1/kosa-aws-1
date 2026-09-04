"""M2 C deterministic deployment-readiness evaluation.

**plan이 Finding을 해소하는지도 여기서 판정한다 (2026-09-05, ADR-0024 §E).** patch를 만든 모델은
"어떤 파일을 어떻게 바꿀지"만 정하고, 그 변경이 실제로 Rule을 만족시키는지는 apply 뒤
재평가에서야 알 수 있었다. plan의 `after` 값에 Assessment와 **같은 술어**를 돌리면 apply 전에
답이 나온다. FAIL이면 승인을 막는다. 답할 수 없으면(plan 근거 없음, Rule을 Catalog가 모름)
아무 신호도 내지 않는다 — "판정 없음"이지 "해소됨"이 아니다. LLM은 이 단계에 없다(ADR-0020 §9).
"""

from __future__ import annotations

from apps.backend.assessment.plan_facts import decide_from_plan_evidence
from packages.contracts import EvaluationStatus, GovernanceControl
from packages.contracts.remediation import (
    DeploymentReadiness,
    DeploymentReadinessStatus,
    PlanReadinessInput,
    RemediationContext,
)

#: plan의 `after` 값이 Finding의 Rule 술어를 여전히 위반한다. 승인하면 위반을 배포한다.
FINDING_UNRESOLVED_IN_PLAN = "FINDING_UNRESOLVED_IN_PLAN"
#: plan의 `after` 값이 술어를 만족한다. 승인 근거가 하나 더 있다는 정보이지 승인 자체가 아니다.
FINDING_RESOLVED_IN_PLAN = "FINDING_RESOLVED_IN_PLAN"


def evaluate_deployment_readiness(
    *,
    context: RemediationContext,
    plan_input: PlanReadinessInput,
    rule_control: tuple[GovernanceControl, tuple[str, ...]] | None = None,
    resource_type: str | None = None,
) -> DeploymentReadiness:
    """Return a non-authorizing readiness verdict for one D-produced plan.

    Non-actionable policy decisions never enter plan generation. This stage
    therefore evaluates only immutable context and plan facts.

    `rule_control`은 Finding의 Rule이 구현하는 Control과 요구 capability다(`control_for_finding`).
    `resource_type`과 함께 있고 `plan_input.plan_evidence`에 이 리소스의 값이 있으면 술어를
    돌린다. 셋 중 하나라도 없으면 그 검사는 건너뛴다 — 건너뛴 사실은 reason code로 남지 않는다.
    """
    if not isinstance(context, RemediationContext):
        raise TypeError("context must be a RemediationContext")
    if not isinstance(plan_input, PlanReadinessInput):
        raise TypeError("plan_input must be a PlanReadinessInput")
    plan = plan_input.plan
    reasons: list[str] = []
    if plan.artifact.customer_id != context.snapshot.customer_id:
        reasons.append("PLAN_CUSTOMER_SCOPE_MISMATCH")
    if plan.artifact.repository_id != context.snapshot.repository_id:
        reasons.append("PLAN_REPOSITORY_SCOPE_MISMATCH")
    if not plan_input.refreshed:
        reasons.append("PLAN_NOT_REFRESHED")
    if context.finding.resource_id not in plan_input.mapped_resource_ids:
        reasons.append("FINDING_RESOURCE_NOT_MAPPED")
    if plan_input.has_destructive_changes:
        reasons.append("DESTRUCTIVE_CHANGE_REQUIRES_MANUAL_REVIEW")

    resolved: bool | None = None
    if rule_control is not None and resource_type is not None:
        control, required = rule_control
        verdict = decide_from_plan_evidence(
            control,
            required,
            resource_type=resource_type,
            resource_id=context.finding.resource_id,
            evidence=plan_input.plan_evidence,
        )
        if verdict is not None:
            resolved = verdict.status is EvaluationStatus.PASS
            if not resolved:
                reasons.append(FINDING_UNRESOLVED_IN_PLAN)

    if any(reason.endswith("MANUAL_REVIEW") for reason in reasons):
        status = DeploymentReadinessStatus.MANUAL_REVIEW
    elif reasons:
        status = DeploymentReadinessStatus.BLOCKED
    else:
        status = DeploymentReadinessStatus.READY_FOR_APPROVAL
        reasons.append("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT")
        if resolved:
            reasons.append(FINDING_RESOLVED_IN_PLAN)
    return DeploymentReadiness(
        deployment_id=plan.deployment_id,
        finding_id=context.finding.finding_id,
        commit_sha=plan.commit_sha,
        plan_hash=plan.plan_hash,
        status=status,
        reason_codes=tuple(reasons),
    )
