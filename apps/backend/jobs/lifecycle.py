"""Deterministic lifecycle policy for persisted Jobs."""

from dataclasses import replace

from apps.backend.jobs.models import Job
from packages.contracts import ApiError, JobCurrentStep, JobStatus


class InvalidJobTransition(ValueError):
    """Raised when a requested Job state change violates the lifecycle."""


class StaleJobRevision(RuntimeError):
    """Raised before persistence when a caller uses an old Job revision."""


_ALLOWED_TRANSITIONS = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.WAITING_REVIEW,
            JobStatus.WAITING_APPROVAL,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.WAITING_REVIEW: frozenset({JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.WAITING_APPROVAL: frozenset(
        {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


def create_job(
    *,
    job_id: str,
    customer_id: str,
    job_type: str,
    initial_step: JobCurrentStep,
    requested_by: str,
    assessment_id: str | None = None,
) -> Job:
    """Create a QUEUED Job with an explicit workflow-owned initial step."""
    return Job(
        job_id=job_id,
        customer_id=customer_id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        current_step=initial_step,
        requested_by=requested_by,
        revision=0,
        assessment_id=assessment_id,
    )


def transition_job(
    job: Job,
    *,
    expected_revision: int,
    status: JobStatus,
    current_step: JobCurrentStep | None = None,
    assessment_id: str | None = None,
    remediation_id: str | None = None,
    deployment_id: str | None = None,
    error: ApiError | None = None,
) -> Job:
    """Return the next immutable Job state after lifecycle validation."""
    if not isinstance(job, Job):
        raise TypeError("job must be a Job")
    _require_revision(expected_revision)
    if job.revision != expected_revision:
        raise StaleJobRevision("job revision does not match expected revision")
    if not isinstance(status, JobStatus):
        raise TypeError("status must be a JobStatus")
    if status not in _ALLOWED_TRANSITIONS[job.status]:
        raise InvalidJobTransition(f"cannot transition from {job.status.value} to {status.value}")
    if current_step is not None and not isinstance(current_step, JobCurrentStep):
        raise TypeError("current_step must be a JobCurrentStep or None")
    if status is not JobStatus.FAILED and error is not None:
        raise InvalidJobTransition("only FAILED jobs may receive an error")

    return replace(
        job,
        status=status,
        current_step=job.current_step if current_step is None else current_step,
        revision=job.revision + 1,
        assessment_id=_link_once(job.assessment_id, assessment_id, "assessment_id"),
        remediation_id=_link_once(job.remediation_id, remediation_id, "remediation_id"),
        deployment_id=_link_once(job.deployment_id, deployment_id, "deployment_id"),
        error=error,
    )


def _link_once(existing: str | None, candidate: str | None, field_name: str) -> str | None:
    if candidate is None or candidate == existing:
        return existing
    if existing is not None:
        raise InvalidJobTransition(f"{field_name} is write-once")
    return candidate


def _require_revision(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected_revision must be an integer")
    if value < 0:
        raise ValueError("expected_revision must be non-negative")
