"""M2 C deterministic deployment-readiness evaluation."""

from packages.contracts.remediation import (
    DeploymentReadiness,
    DeploymentReadinessStatus,
    PlanReadinessInput,
    RemediationContext,
    RemediationStrategy,
)


def evaluate_deployment_readiness(
    *, context: RemediationContext, plan_input: PlanReadinessInput
) -> DeploymentReadiness:
    """Return a non-authorizing readiness verdict for one D-produced plan."""
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
    if context.strategy is RemediationStrategy.MANUAL_REVIEW:
        reasons.append("REMEDIATION_STRATEGY_REQUIRES_MANUAL_REVIEW")

    if any(reason.endswith("MANUAL_REVIEW") for reason in reasons):
        status = DeploymentReadinessStatus.MANUAL_REVIEW
    elif reasons:
        status = DeploymentReadinessStatus.BLOCKED
    else:
        status = DeploymentReadinessStatus.READY_FOR_APPROVAL
        reasons.append("REFRESHED_PLAN_BOUND_TO_REMEDIATION_CONTEXT")
    return DeploymentReadiness(
        deployment_id=plan.deployment_id,
        finding_id=context.finding.finding_id,
        commit_sha=plan.commit_sha,
        plan_hash=plan.plan_hash,
        status=status,
        reason_codes=tuple(reasons),
    )
