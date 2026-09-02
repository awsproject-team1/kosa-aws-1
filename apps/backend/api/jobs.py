"""Injected application service for the M0 Job HTTP boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from apps.backend.assessment import Assessment
from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import (
    JobNotFoundError,
    OutboxDispatcher,
    WorkflowOutboxEntry,
    authorize_job_read,
    create_job,
)
from apps.backend.repositories import AssessmentWorkflowRepository
from packages.contracts import (
    AssessmentPhase,
    JobCurrentStep,
    JobResponse,
    WorkflowCommand,
    WorkflowTask,
)


class AssessmentScope(Protocol):
    """Verify customer-owned selectors before a Job is persisted or dispatched."""

    def authorize(
        self, principal: Principal, *, repository_id: str, policy_profile_id: str
    ) -> None:
        """Raise AssessmentScopeDenied unless both selectors are approved for the principal."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentRequest:
    """Client-provided selectors permitted at the assessment creation boundary."""

    repository_id: str
    policy_profile_id: str

    def __post_init__(self) -> None:
        _require_non_empty_string(self.repository_id, "repository_id")
        _require_non_empty_string(self.policy_profile_id, "policy_profile_id")


class JobApiService:
    """Create and read Jobs without accepting tenant or lifecycle fields from callers."""

    def __init__(
        self,
        *,
        repository: AssessmentWorkflowRepository,
        assessment_scope: AssessmentScope,
        outbox_dispatcher: OutboxDispatcher,
        job_id_factory: Callable[[], str],
        assessment_id_factory: Callable[[], str],
    ) -> None:
        self._repository = repository
        self._assessment_scope = assessment_scope
        if not isinstance(outbox_dispatcher, OutboxDispatcher):
            raise TypeError("outbox_dispatcher must be an OutboxDispatcher")
        self._outbox_dispatcher = outbox_dispatcher
        self._job_id_factory = job_id_factory
        self._assessment_id_factory = assessment_id_factory

    def create_assessment(self, principal: Principal, request: AssessmentRequest) -> JobResponse:
        """Persist a customer-scoped queued Job then dispatch its internal worker command."""
        _require_principal_and_request(principal, request)
        authorize(principal, Action.START_ASSESSMENT)
        self._assessment_scope.authorize(
            principal,
            repository_id=request.repository_id,
            policy_profile_id=request.policy_profile_id,
        )
        job_id = _new_job_id(self._job_id_factory)
        assessment = Assessment(
            assessment_id=_new_assessment_id(self._assessment_id_factory),
            customer_id=principal.customer_id,
            job_id=job_id,
            repository_id=request.repository_id,
            policy_profile_id=request.policy_profile_id,
            phase=AssessmentPhase.INITIAL,
        )
        job = create_job(
            job_id=job_id,
            customer_id=principal.customer_id,
            job_type="ASSESSMENT",
            initial_step=JobCurrentStep.LOAD_IAC,
            requested_by=principal.subject,
            assessment_id=assessment.assessment_id,
        )
        outbox = WorkflowOutboxEntry(
            customer_id=principal.customer_id,
            job_id=job.job_id,
            task=WorkflowTask(
                job_id=job.job_id,
                expected_revision=job.revision,
                command=WorkflowCommand.ASSESS_RESOURCE,
            ),
        )
        self._repository.create_assessment_workflow(
            assessment,
            job,
            outbox,
        )
        # The transactional Outbox is durable before this best-effort send. On a
        # queue or bookkeeping failure the scheduled sweeper retries the entry.
        self._outbox_dispatcher.dispatch_entry(outbox)
        return job.to_response()

    def get_job(self, principal: Principal, job_id: str) -> JobResponse:
        """Read through the customer base key before applying owner/admin authorization."""
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        _require_non_empty_string(job_id, "job_id")
        authorize(principal, Action.READ_JOB)
        job = self._repository.get_job(principal.customer_id, job_id)
        if job is None:
            raise JobNotFoundError("job not found")
        authorize_job_read(principal, job)
        return job.to_response()


def _new_job_id(factory: Callable[[], str]) -> str:
    if not callable(factory):
        raise TypeError("job_id_factory must be callable")
    job_id = factory()
    _require_non_empty_string(job_id, "generated job_id")
    return job_id


def _new_assessment_id(factory: Callable[[], str]) -> str:
    if not callable(factory):
        raise TypeError("assessment_id_factory must be callable")
    assessment_id = factory()
    _require_non_empty_string(assessment_id, "generated assessment_id")
    return assessment_id


def _require_principal_and_request(principal: object, request: object) -> None:
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    if not isinstance(request, AssessmentRequest):
        raise TypeError("request must be an AssessmentRequest")


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
