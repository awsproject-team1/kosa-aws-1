"""Workflow Job transport contracts."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import (
    require_non_empty_string,
    require_optional_non_empty_string,
)
from packages.contracts.errors import ApiError


class JobStatus(StrEnum):
    """Lifecycle states shared by Job producers and consumers."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobCurrentStep(StrEnum):
    """Currently approved workflow steps exposed by Job polling."""

    LOAD_IAC = "LOAD_IAC"
    LOAD_POLICY_PROFILE = "LOAD_POLICY_PROFILE"
    BUILD_EFFECTIVE_RULES = "BUILD_EFFECTIVE_RULES"
    LOAD_POLICY_EVIDENCE = "LOAD_POLICY_EVIDENCE"
    ASSESS = "ASSESS"
    POLICY_REVIEW = "POLICY_REVIEW"
    GENERATE_FINDINGS = "GENERATE_FINDINGS"
    GENERATE_REPORT = "GENERATE_REPORT"
    GENERATE_REMEDIATION = "GENERATE_REMEDIATION"
    CREATE_PR = "CREATE_PR"
    CI_VALIDATION = "CI_VALIDATION"
    AWS_DISCOVERY = "AWS_DISCOVERY"
    PRE_DEPLOY_VALIDATION = "PRE_DEPLOY_VALIDATION"
    TERRAFORM_PLAN = "TERRAFORM_PLAN"
    APPLY = "APPLY"
    POST_DEPLOY_VERIFICATION = "POST_DEPLOY_VERIFICATION"


@dataclass(frozen=True, slots=True, kw_only=True)
class JobResponse:
    """Public polling projection for one workflow Job."""

    job_id: str
    job_type: str
    status: JobStatus
    current_step: JobCurrentStep
    assessment_id: str | None = None
    remediation_id: str | None = None
    deployment_id: str | None = None
    error: ApiError | None = None

    def __post_init__(self) -> None:
        require_non_empty_string(self.job_id, "job_id")
        require_non_empty_string(self.job_type, "job_type")
        if not isinstance(self.status, JobStatus):
            raise TypeError("status must be a JobStatus")
        if not isinstance(self.current_step, JobCurrentStep):
            raise TypeError("current_step must be a JobCurrentStep")
        require_optional_non_empty_string(self.assessment_id, "assessment_id")
        require_optional_non_empty_string(self.remediation_id, "remediation_id")
        require_optional_non_empty_string(self.deployment_id, "deployment_id")
        if self.error is not None and not isinstance(self.error, ApiError):
            raise TypeError("error must be an ApiError or None")

    def to_dict(self) -> dict[str, object]:
        """Return the complete public Job polling wire shape."""
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": self.status.value,
            "current_step": self.current_step.value,
            "assessment_id": self.assessment_id,
            "remediation_id": self.remediation_id,
            "deployment_id": self.deployment_id,
            "error": None if self.error is None else self.error.to_dict(),
        }
