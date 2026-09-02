"""A-owned approval gate over C readiness and D's immutable Terraform plan."""

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from packages.contracts import DeploymentApproval, TerraformPlan
from packages.contracts.remediation import DeploymentReadiness, DeploymentReadinessStatus


class DeploymentApprovalError(ValueError):
    """Raised when a deployment cannot enter the human-approval state."""


class DeploymentConflictError(ValueError):
    """Raised when a deployment cannot be created from the stored remediation."""


class DeploymentApprovalRepository(Protocol):
    """A persistence seam; implementations must conditionally write exact bindings."""

    def record_approval(
        self,
        *,
        customer_id: str,
        approval: DeploymentApproval,
        readiness: DeploymentReadiness,
    ) -> None:
        """Persist an immutable approval and its audit record atomically."""
        ...


class DeploymentApprovalService:
    """Authorize and persist only a C-ready approval bound to D's exact plan."""

    def __init__(self, repository: DeploymentApprovalRepository) -> None:
        if repository is None:
            raise TypeError("repository is required")
        self._repository = repository

    def approve(
        self,
        *,
        principal: Principal,
        plan: TerraformPlan,
        readiness: DeploymentReadiness,
    ) -> DeploymentApproval:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        if not isinstance(readiness, DeploymentReadiness):
            raise TypeError("readiness must be a DeploymentReadiness")
        authorize(principal, Action.APPROVE_DEPLOYMENT)
        if plan.plan_hash != plan.artifact.content_sha256:
            raise DeploymentApprovalError("Terraform plan digest is not exact")
        if plan.artifact.customer_id != principal.customer_id:
            raise DeploymentApprovalError("plan is outside the principal customer scope")
        if readiness.status is not DeploymentReadinessStatus.READY_FOR_APPROVAL:
            raise DeploymentApprovalError("deployment readiness is not approvable")
        if (
            readiness.deployment_id != plan.deployment_id
            or readiness.commit_sha != plan.commit_sha
            or readiness.plan_hash != plan.plan_hash
        ):
            raise DeploymentApprovalError("readiness is not bound to the exact Terraform plan")
        approval = DeploymentApproval(
            deployment_id=plan.deployment_id,
            approved_by=principal.subject,
            commit_sha=plan.commit_sha,
            plan_hash=plan.plan_hash,
        )
        self._repository.record_approval(
            customer_id=principal.customer_id, approval=approval, readiness=readiness
        )
        return approval
