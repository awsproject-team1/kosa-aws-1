"""Durable selector state for an Assessment workflow."""

from dataclasses import dataclass

from packages.contracts import AssessmentPhase


@dataclass(frozen=True, slots=True, kw_only=True)
class Assessment:
    """Assessment selectors and immutable phase provenance persisted before dispatch."""

    assessment_id: str
    customer_id: str
    job_id: str
    repository_id: str
    policy_profile_id: str
    phase: AssessmentPhase = AssessmentPhase.INITIAL
    source_assessment_id: str | None = None
    deployment_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "assessment_id",
            "customer_id",
            "job_id",
            "repository_id",
            "policy_profile_id",
        ):
            _require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        for name in ("source_assessment_id", "deployment_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_string(value, name)
        has_source = self.source_assessment_id is not None
        has_deployment = self.deployment_id is not None
        if self.phase is AssessmentPhase.POST_DEPLOY_VERIFICATION:
            if not has_source or not has_deployment:
                raise ValueError(
                    "POST_DEPLOY_VERIFICATION requires source_assessment_id and deployment_id"
                )
            if self.source_assessment_id == self.assessment_id:
                raise ValueError("verification assessment must differ from source assessment")
        elif has_source or has_deployment:
            raise ValueError(
                "source_assessment_id and deployment_id are only valid for post-deploy verification"
            )


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
