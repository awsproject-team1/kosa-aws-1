"""M2 A approval API and M3 A deployment creation over stored inputs."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from apps.backend.assessment.comparison import (
    ComparisonAssessment,
    compare_post_deploy_assessments,
)
from apps.backend.auth import Action, Principal, authorize
from apps.backend.deployment import (
    DeploymentApprovalError,
    DeploymentApprovalService,
    DeploymentConflictError,
    DeploymentRecord,
    DeploymentRecordRepository,
    DeploymentRejection,
)
from apps.backend.jobs import JobNotFoundError, OutboxDispatcher, WorkflowOutboxEntry, create_job
from apps.backend.jobs.errors import RequestValidationError
from apps.backend.jobs.lifecycle import transition_job
from apps.backend.jobs.models import Job
from apps.backend.repositories import JobRepository
from packages.contracts import (
    AssessmentComparison,
    DeploymentApproval,
    DeploymentFacts,
    DeploymentRejectionReason,
    DeploymentStatus,
    JobCurrentStep,
    JobStatus,
    RemediationAction,
    TerraformPlan,
    WorkflowCommand,
    WorkflowTask,
    derive_deployment_status,
)
from packages.contracts.remediation import DeploymentReadiness


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentSource:
    """The stored remediation facts a deployment is created from (ADR-0019 §4)."""

    remediation_id: str
    customer_id: str
    repository_id: str
    commit_sha: str
    source_assessment_id: str
    action: RemediationAction
    has_worker_result: bool
    commit_reachable_from_default_branch: bool

    def __post_init__(self) -> None:
        for value, name in (
            (self.remediation_id, "remediation_id"),
            (self.customer_id, "customer_id"),
            (self.repository_id, "repository_id"),
            (self.commit_sha, "commit_sha"),
            (self.source_assessment_id, "source_assessment_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.action, RemediationAction):
            raise TypeError("action must be a RemediationAction")
        for flag, name in (
            (self.has_worker_result, "has_worker_result"),
            (self.commit_reachable_from_default_branch, "commit_reachable_from_default_branch"),
        ):
            if not isinstance(flag, bool):
                raise TypeError(f"{name} must be a bool")


class DeploymentSourceReader(Protocol):
    def get_deployment_source(
        self, *, customer_id: str, remediation_id: str
    ) -> DeploymentSource: ...


class DeploymentPlanReader(Protocol):
    def get_approval_input(
        self, *, customer_id: str, deployment_id: str
    ) -> tuple[TerraformPlan, DeploymentReadiness]: ...


class DeploymentFactsReader(Protocol):
    def get_deployment_facts(self, *, customer_id: str, deployment_id: str) -> DeploymentFacts: ...


class ComparisonInputReader(Protocol):
    def get_comparison_inputs(
        self, *, customer_id: str, source_assessment_id: str, verification_assessment_id: str
    ) -> tuple[ComparisonAssessment, ComparisonAssessment]:
        """Return complete (source, verification) inputs, raising on incomplete data."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentView:
    """Public read projection for one deployment (ADR-0019 §8)."""

    deployment_id: str
    status: DeploymentStatus
    commit_sha: str
    remediation_id: str
    source_assessment_id: str
    plan_hash: str | None
    verification_assessment_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "status": self.status.value,
            "commit_sha": self.commit_sha,
            "remediation_id": self.remediation_id,
            "source_assessment_id": self.source_assessment_id,
            "plan_hash": self.plan_hash,
            "verification_assessment_id": self.verification_assessment_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentApprovalRequest:
    commit_sha: str
    plan_hash: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(item, str) and item.strip() for item in (self.commit_sha, self.plan_hash)
        ):
            raise ValueError("commit_sha and plan_hash must be non-empty strings")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentRejectRequest:
    reason: DeploymentRejectionReason
    ticket_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, DeploymentRejectionReason):
            raise TypeError("reason must be a DeploymentRejectionReason")
        if self.ticket_reference is not None and (
            not isinstance(self.ticket_reference, str) or not self.ticket_reference.strip()
        ):
            raise ValueError("ticket_reference must be a non-empty string when present")


class DeploymentApiService:
    def __init__(
        self,
        *,
        approvals: DeploymentApprovalService,
        plans: DeploymentPlanReader | None = None,
        sources: DeploymentSourceReader | None = None,
        deployments: DeploymentRecordRepository | None = None,
        facts: DeploymentFactsReader | None = None,
        comparisons: ComparisonInputReader | None = None,
        jobs: JobRepository | None = None,
        outbox_dispatcher: OutboxDispatcher | None = None,
        deployment_id_factory: Callable[[], str] | None = None,
        job_id_factory: Callable[[], str] | None = None,
        now: Callable[[], object] | None = None,
    ) -> None:
        if not isinstance(approvals, DeploymentApprovalService):
            raise TypeError("approvals is required")
        self._plans, self._approvals = plans, approvals
        self._sources = sources
        self._deployments = deployments
        self._facts = facts
        self._comparisons = comparisons
        self._jobs = jobs
        self._outbox_dispatcher = outbox_dispatcher
        self._deployment_id_factory = deployment_id_factory
        self._job_id_factory = job_id_factory
        self._now = now

    def reject(
        self, principal: Principal, deployment_id: str, request: "DeploymentRejectRequest"
    ) -> DeploymentRejection:
        """Admin-only terminal reject: cancel the Job and audit it (ADR-0019 §8)."""
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(request, DeploymentRejectRequest):
            raise TypeError("request must be a DeploymentRejectRequest")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id must be a non-empty string")
        if self._deployments is None or self._jobs is None or self._now is None:
            raise TypeError("deployment reject dependencies are not configured")
        authorize(principal, Action.REJECT_DEPLOYMENT)
        record = self._deployments.get_deployment(
            customer_id=principal.customer_id, deployment_id=deployment_id
        )
        if record is None:
            raise JobNotFoundError("deployment not found")
        job = self._jobs.get_job(principal.customer_id, record.job_id)
        if job is None:
            raise JobNotFoundError("deployment job not found")
        try:
            cancelled = transition_job(
                job,
                expected_revision=job.revision,
                status=JobStatus.CANCELLED,
                current_step=job.current_step,
                deployment_id=job.deployment_id,
            )
        except Exception as error:
            raise DeploymentConflictError("deployment cannot be rejected in its state") from error
        rejected_at = self._now()
        rejection = DeploymentRejection(
            deployment_id=deployment_id,
            rejected_by=principal.subject,
            reason=request.reason,
            rejected_at=rejected_at.isoformat(),  # type: ignore[attr-defined]
            ticket_reference=request.ticket_reference,
        )
        self._deployments.reject_deployment(
            rejection=rejection, cancelled_job=cancelled, expected_revision=job.revision
        )
        return rejection

    def get_verification(self, principal: Principal, deployment_id: str) -> AssessmentComparison:
        """Return the before/after comparison for a verified deployment (ADR-0020 §1, §7)."""
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id must be a non-empty string")
        if self._deployments is None or self._comparisons is None:
            raise TypeError("verification read dependencies are not configured")
        authorize(principal, Action.READ_JOB)
        record = self._deployments.get_deployment(
            customer_id=principal.customer_id, deployment_id=deployment_id
        )
        if record is None:
            raise JobNotFoundError("deployment not found")
        if record.verification_assessment_id is None:
            # The verification Assessment is created only after apply completes.
            raise JobNotFoundError("deployment has no verification assessment yet")
        try:
            source, verification = self._comparisons.get_comparison_inputs(
                customer_id=principal.customer_id,
                source_assessment_id=record.source_assessment_id,
                verification_assessment_id=record.verification_assessment_id,
            )
            if not isinstance(source, ComparisonAssessment) or not isinstance(
                verification, ComparisonAssessment
            ):
                raise TypeError("comparison inputs must be ComparisonAssessment values")
            return compare_post_deploy_assessments(
                deployment_id=deployment_id, source=source, verification=verification
            )
        except (TypeError, ValueError) as error:
            # Incomplete or corrupt comparison input is a validation error, not a 500
            # (ADR-0020 §5). This is distinct from a comparable=false result, which is
            # a normal 200 body returned by the comparison itself.
            raise RequestValidationError("verification comparison input is invalid") from error

    def get_deployment(self, principal: Principal, deployment_id: str) -> DeploymentView:
        """Return the deployment's derived status and identity (ADR-0019 §8)."""
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id must be a non-empty string")
        if self._deployments is None or self._facts is None:
            raise TypeError("deployment read dependencies are not configured")
        authorize(principal, Action.READ_JOB)
        record = self._deployments.get_deployment(
            customer_id=principal.customer_id, deployment_id=deployment_id
        )
        if record is None:
            raise JobNotFoundError("deployment not found")
        facts = self._facts.get_deployment_facts(
            customer_id=principal.customer_id, deployment_id=deployment_id
        )
        if not isinstance(facts, DeploymentFacts):
            raise TypeError("facts reader must return a DeploymentFacts")
        return DeploymentView(
            deployment_id=record.deployment_id,
            status=derive_deployment_status(facts),
            commit_sha=record.commit_sha,
            remediation_id=record.remediation_id,
            source_assessment_id=record.source_assessment_id,
            plan_hash=record.plan_hash,
            verification_assessment_id=record.verification_assessment_id,
        )

    def create_deployment(self, principal: Principal, remediation_id: str) -> Job:
        """Create a deployment from a stored actionable remediation (ADR-0019 §4)."""
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(remediation_id, str) or not remediation_id.strip():
            raise ValueError("remediation_id must be a non-empty string")
        if (
            self._sources is None
            or self._deployments is None
            or self._outbox_dispatcher is None
            or self._deployment_id_factory is None
            or self._job_id_factory is None
        ):
            raise TypeError("deployment creation dependencies are not configured")
        authorize(principal, Action.START_DEPLOYMENT)
        source = self._sources.get_deployment_source(
            customer_id=principal.customer_id, remediation_id=remediation_id
        )
        if source.customer_id != principal.customer_id:
            raise DeploymentConflictError("remediation is outside the principal customer scope")
        if source.action not in (RemediationAction.TERRAFORM_PATCH, RemediationAction.ACTUAL_SYNC):
            raise DeploymentConflictError("remediation decision is not deployable")
        if not source.has_worker_result:
            raise DeploymentConflictError("remediation has no worker result to deploy")
        if (
            source.action is RemediationAction.TERRAFORM_PATCH
            and not source.commit_reachable_from_default_branch
        ):
            raise DeploymentConflictError("target commit is not reachable from the default branch")

        deployment_id = self._new(self._deployment_id_factory, "deployment")
        job_id = self._new(self._job_id_factory, "job")
        job = create_job(
            job_id=job_id,
            customer_id=principal.customer_id,
            job_type="DEPLOYMENT",
            initial_step=JobCurrentStep.TERRAFORM_PLAN,
            requested_by=principal.subject,
        )
        job = replace(job, deployment_id=deployment_id)
        record = DeploymentRecord(
            deployment_id=deployment_id,
            customer_id=principal.customer_id,
            repository_id=source.repository_id,
            job_id=job_id,
            remediation_id=remediation_id,
            commit_sha=source.commit_sha,
            source_assessment_id=source.source_assessment_id,
        )
        outbox = WorkflowOutboxEntry(
            customer_id=principal.customer_id,
            job_id=job_id,
            task=WorkflowTask(
                job_id=job_id, expected_revision=0, command=WorkflowCommand.RUN_DEPLOYMENT
            ),
        )
        self._deployments.create_deployment(record, job=job, outbox=outbox)
        self._outbox_dispatcher.dispatch_entry(outbox)
        return job

    @staticmethod
    def _new(factory: Callable[[], str], name: str) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"generated {name}_id must be a non-empty string")
        return value

    def approve(
        self, principal: Principal, deployment_id: str, request: DeploymentApprovalRequest
    ) -> DeploymentApproval:
        if not isinstance(principal, Principal) or not isinstance(
            request, DeploymentApprovalRequest
        ):
            raise TypeError("principal and request are required")
        if self._plans is None:
            raise TypeError("approval plan reader is not configured")
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
