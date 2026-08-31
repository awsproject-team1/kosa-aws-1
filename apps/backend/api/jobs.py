"""Injected application service for the M0 Job HTTP boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from apps.backend.assessment import Assessment
from apps.backend.auth import Action, Principal, authorize
from apps.backend.jobs import (
    JobNotFoundError,
    WorkflowDispatchError,
    authorize_job_read,
    create_job,
    transition_job,
)
from apps.backend.repositories import AssessmentRepository, JobRepository
from packages.contracts import (
    ApiError,
    JobCurrentStep,
    JobResponse,
    JobStatus,
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


class WorkflowDispatcher(Protocol):
    """Publish a minimal, resumable WorkflowTask to the selected internal queue."""

    def dispatch(self, task: WorkflowTask) -> None:
        """Deliver a task without exposing queue details to the public API."""
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
        repository: JobRepository,
        assessment_repository: AssessmentRepository,
        assessment_scope: AssessmentScope,
        dispatcher: WorkflowDispatcher,
        job_id_factory: Callable[[], str],
        assessment_id_factory: Callable[[], str],
    ) -> None:
        self._repository = repository
        self._assessment_repository = assessment_repository
        self._assessment_scope = assessment_scope
        self._dispatcher = dispatcher
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
        )
        job = create_job(
            job_id=job_id,
            customer_id=principal.customer_id,
            job_type="ASSESSMENT",
            initial_step=JobCurrentStep.LOAD_IAC,
            requested_by=principal.subject,
            assessment_id=assessment.assessment_id,
        )
        self._assessment_repository.create_assessment(assessment)
        self._repository.create_job(job)
        try:
            self._dispatcher.dispatch(
                WorkflowTask(
                    job_id=job.job_id,
                    expected_revision=job.revision,
                    command=WorkflowCommand.ASSESS_RESOURCE,
                )
            )
        except Exception:
            failed_job = transition_job(
                job,
                expected_revision=job.revision,
                status=JobStatus.FAILED,
                error=ApiError(
                    code="EXECUTION_ERROR",
                    message="Workflow dispatch failed before execution began",
                ),
            )
            try:
                self._repository.update_job(failed_job, expected_revision=job.revision)
            except Exception:
                pass
            raise WorkflowDispatchError("workflow dispatch failed") from None
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
