"""A-owned approval gate over C readiness and D's immutable Terraform plan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.deployment.record import DeploymentRecord
from packages.contracts import (
    DeploymentApproval,
    TerraformPlan,
)
from packages.contracts.remediation import DeploymentReadiness, DeploymentReadinessStatus

if TYPE_CHECKING:
    from apps.backend.jobs.models import Job
    from apps.backend.jobs.outbox import OutboxDispatcher, WorkflowOutboxEntry


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
        resumed_job: Job,
        expected_revision: int,
        outbox: WorkflowOutboxEntry,
    ) -> None:
        """Persist approval, resumed Job, and apply-dispatch outbox atomically."""
        ...

    def get_approval(self, *, customer_id: str, deployment_id: str) -> DeploymentApproval | None:
        """Return the stored approval for a deployment, or None when it is absent.

        D의 Deployment Worker가 apply 계열 command에서 승인 사실을 다시 읽어 검증하는 read
        경로다(ADR-0019 §5·§7). 승인은 A가 record하는 사실이며 D는 만들지 않고 대조만 한다.
        """
        ...


class DeploymentJobLookup(Protocol):
    def get_job(self, customer_id: str, job_id: str) -> Job | None: ...


class DeploymentLookup(Protocol):
    def get_deployment(
        self, *, customer_id: str, deployment_id: str
    ) -> DeploymentRecord | None: ...


class DeploymentApprovalService:
    """Authorize and persist only a C-ready approval bound to D's exact plan."""

    def __init__(
        self,
        repository: DeploymentApprovalRepository,
        *,
        deployments: DeploymentLookup | None = None,
        jobs: DeploymentJobLookup | None = None,
        outbox_dispatcher: OutboxDispatcher | None = None,
    ) -> None:
        if repository is None:
            raise TypeError("repository is required")
        configured = (deployments, jobs, outbox_dispatcher)
        if any(value is None for value in configured) and any(
            value is not None for value in configured
        ):
            raise TypeError("deployments, jobs, and outbox_dispatcher must be configured together")
        self._repository = repository
        self._deployments = deployments
        self._jobs = jobs
        self._outbox_dispatcher = outbox_dispatcher

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
        if self._deployments is None or self._jobs is None or self._outbox_dispatcher is None:
            raise DeploymentApprovalError("approval apply-dispatch boundary is not configured")
        # `apps.backend.jobs` imports this module's error vocabulary.  Keep the
        # lifecycle imports at the execution boundary to avoid a package-init cycle.
        from apps.backend.jobs.lifecycle import transition_job
        from apps.backend.jobs.outbox import WorkflowOutboxEntry
        from packages.contracts import JobCurrentStep, JobStatus, WorkflowCommand, WorkflowTask

        deployment = self._deployments.get_deployment(
            customer_id=principal.customer_id, deployment_id=plan.deployment_id
        )
        if deployment is None:
            raise DeploymentApprovalError("deployment is not available for apply")
        job = self._jobs.get_job(principal.customer_id, deployment.job_id)
        if job is None:
            raise DeploymentApprovalError("deployment job is not available for apply")
        expected_revision = job.revision
        resumed = transition_job(
            job,
            expected_revision=expected_revision,
            status=JobStatus.RUNNING,
            current_step=JobCurrentStep.APPLY,
        )
        outbox = WorkflowOutboxEntry(
            customer_id=principal.customer_id,
            job_id=job.job_id,
            task=WorkflowTask(
                job_id=job.job_id,
                expected_revision=resumed.revision,
                command=WorkflowCommand.PLAN_COMPLETED,
            ),
        )
        self._repository.record_approval(
            customer_id=principal.customer_id,
            approval=approval,
            readiness=readiness,
            resumed_job=resumed,
            expected_revision=expected_revision,
            outbox=outbox,
        )
        self._outbox_dispatcher.dispatch_entry(outbox)
        return approval
