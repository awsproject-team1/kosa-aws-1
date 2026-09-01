"""M2 A approval API over stored D plan and C readiness inputs."""

from dataclasses import dataclass
from typing import Protocol

from apps.backend.auth import Principal
from apps.backend.deployment import DeploymentApprovalError, DeploymentApprovalService
from packages.contracts import DeploymentApproval, TerraformPlan
from packages.contracts.remediation import DeploymentReadiness


class DeploymentPlanReader(Protocol):
    def get_approval_input(
        self, *, customer_id: str, deployment_id: str
    ) -> tuple[TerraformPlan, DeploymentReadiness]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentApprovalRequest:
    commit_sha: str
    plan_hash: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, str) and item.strip() for item in (self.commit_sha, self.plan_hash)
        ):
            raise ValueError("commit_sha and plan_hash must be non-empty strings")


class DeploymentApiService:
    def __init__(
        self, *, plans: DeploymentPlanReader, approvals: DeploymentApprovalService
    ) -> None:
        if plans is None or not isinstance(approvals, DeploymentApprovalService):
            raise TypeError("plans and approvals are required")
        self._plans, self._approvals = plans, approvals

    def approve(
        self, principal: Principal, deployment_id: str, request: DeploymentApprovalRequest
    ) -> DeploymentApproval:
        if not isinstance(principal, Principal) or not isinstance(
            request, DeploymentApprovalRequest
        ):
            raise TypeError("principal and request are required")
        plan, readiness = self._plans.get_approval_input(
            customer_id=principal.customer_id, deployment_id=deployment_id
        )
        if plan.deployment_id != deployment_id or (request.commit_sha, request.plan_hash) != (
            plan.commit_sha,
            plan.plan_hash,
        ):
            raise DeploymentApprovalError(
                "approval request does not match the stored Terraform plan"
            )
        return self._approvals.approve(principal=principal, plan=plan, readiness=readiness)
