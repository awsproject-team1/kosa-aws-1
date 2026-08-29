"""Immutable Backend Job state and public polling projection."""

from dataclasses import dataclass

from packages.contracts import ApiError, JobCurrentStep, JobResponse, JobStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class Job:
    """Persisted Backend state for one workflow Job."""

    job_id: str
    customer_id: str
    job_type: str
    status: JobStatus
    current_step: JobCurrentStep
    requested_by: str
    revision: int
    assessment_id: str | None = None
    remediation_id: str | None = None
    deployment_id: str | None = None
    error: ApiError | None = None

    def __post_init__(self) -> None:
        _require_non_empty_string(self.job_id, "job_id")
        _require_non_empty_string(self.customer_id, "customer_id")
        _require_non_empty_string(self.job_type, "job_type")
        _require_non_empty_string(self.requested_by, "requested_by")
        if not isinstance(self.status, JobStatus):
            raise TypeError("status must be a JobStatus")
        if not isinstance(self.current_step, JobCurrentStep):
            raise TypeError("current_step must be a JobCurrentStep")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        _require_optional_non_empty_string(self.assessment_id, "assessment_id")
        _require_optional_non_empty_string(self.remediation_id, "remediation_id")
        _require_optional_non_empty_string(self.deployment_id, "deployment_id")
        if self.status is JobStatus.FAILED:
            if not isinstance(self.error, ApiError):
                raise ValueError("FAILED jobs require an ApiError")
        elif self.error is not None:
            raise ValueError("only FAILED jobs may contain an error")

    def to_response(self) -> JobResponse:
        """Project internal state to the approved public polling contract."""
        return JobResponse(
            job_id=self.job_id,
            job_type=self.job_type,
            status=self.status,
            current_step=self.current_step,
            assessment_id=self.assessment_id,
            remediation_id=self.remediation_id,
            deployment_id=self.deployment_id,
            error=self.error,
        )


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_optional_non_empty_string(value: object, field_name: str) -> None:
    if value is not None:
        _require_non_empty_string(value, field_name)
