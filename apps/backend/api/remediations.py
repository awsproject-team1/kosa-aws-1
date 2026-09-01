"""M2 A public-service boundary for starting a remediation workflow."""

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import OutboxDispatcher, WorkflowOutboxEntry, create_job
from apps.backend.jobs.models import Job
from apps.backend.repositories import RepositoryError
from packages.contracts import JobCurrentStep, JobResponse, WorkflowCommand, WorkflowTask
from packages.contracts.remediation import RemediationContext


class RemediationContextReader(Protocol):
    def get_context(self, *, customer_id: str, finding_id: str) -> RemediationContext: ...


class RemediationWorkflowRepository(Protocol):
    def create_remediation_workflow(
        self,
        *,
        context: RemediationContext,
        job: Job,
        remediation_id: str,
        outbox: WorkflowOutboxEntry,
    ) -> None: ...


class RemediationApiService:
    """Create a customer-scoped remediation Job without accepting lifecycle fields."""

    def __init__(
        self,
        *,
        contexts: RemediationContextReader,
        repository: RemediationWorkflowRepository,
        outbox_dispatcher: OutboxDispatcher,
        job_id_factory: Callable[[], str],
        remediation_id_factory: Callable[[], str],
    ) -> None:
        self._contexts, self._repository, self._outbox_dispatcher = (
            contexts,
            repository,
            outbox_dispatcher,
        )
        self._job_id_factory, self._remediation_id_factory = job_id_factory, remediation_id_factory

    def create_remediation(self, principal: Principal, finding_id: str) -> JobResponse:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError("finding_id must be a non-empty string")
        authorize(principal, Action.START_REMEDIATION)
        context = self._contexts.get_context(
            customer_id=principal.customer_id, finding_id=finding_id
        )
        if (
            context.finding.finding_id != finding_id
            or context.snapshot.customer_id != principal.customer_id
        ):
            raise RepositoryError("remediation context is outside the requested scope")
        job_id, remediation_id = (
            self._new(self._job_id_factory, "job"),
            self._new(self._remediation_id_factory, "remediation"),
        )
        job = create_job(
            job_id=job_id,
            customer_id=principal.customer_id,
            job_type="REMEDIATION",
            initial_step=JobCurrentStep.GENERATE_REMEDIATION,
            requested_by=principal.subject,
        )
        job = replace(job, remediation_id=remediation_id)
        outbox = WorkflowOutboxEntry(
            customer_id=principal.customer_id,
            job_id=job_id,
            task=WorkflowTask(
                job_id=job_id, expected_revision=0, command=WorkflowCommand.GENERATE_REMEDIATION
            ),
        )
        self._repository.create_remediation_workflow(
            context=context, job=job, remediation_id=remediation_id, outbox=outbox
        )
        self._outbox_dispatcher.dispatch_entry(outbox)
        return job.to_response()

    @staticmethod
    def _new(factory: Callable[[], str], name: str) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"generated {name}_id must be a non-empty string")
        return value
