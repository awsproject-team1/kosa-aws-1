"""Fix a Post-Deploy Verification Assessment to the scope it verifies (ADR-0020 §2·§3).

A verification is not a new decision about what to evaluate. It reuses the source
Assessment's Repository, Policy Profile **version**, planned
`(resource_id, rule_id, perspective)` set, Model Profile, and rubric, so that a
score or Finding change can only be attributed to the infrastructure change.

This boundary is pure: it reads and writes nothing. The Deployment owner resolves
the durable facts and the pinned Policy Context, calls this, and persists the
returned Assessment in the same transaction that advances the Deployment Job
(ADR-0020 §7). Rejections are values with a code, not storage failures, because
"the Profile was replaced" is a routing decision — that case becomes a new Initial
Assessment rather than a verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from apps.backend.assessment.models import Assessment
from apps.backend.policy import PolicyContext
from packages.contracts import AssessmentPhase, PlannedEvaluation


class VerificationRejectionCode(StrEnum):
    """Why a Post-Deploy Verification cannot reuse the source Assessment scope.

    These values stay inside the application boundary. The verification endpoint
    that would render them presumes a Deployment record and is blocked on the
    ADR-0019 signature, and publishing an API vocabulary before that decision is
    the mistake `AuditEventType` deliberately avoided.
    """

    POLICY_PROFILE_MISMATCH = "POLICY_PROFILE_MISMATCH"
    """The resolved Context is a different Profile than the source Assessment used."""

    POLICY_PROFILE_VERSION_REPLACED = "POLICY_PROFILE_VERSION_REPLACED"
    """The Profile version changed; ADR-0020 §2 routes this to a new Initial Assessment."""

    CONTEXT_PHASE_MISMATCH = "CONTEXT_PHASE_MISMATCH"
    """The Context was not resolved for `POST_DEPLOY_VERIFICATION`."""

    PLANNED_RULE_NOT_APPLICABLE = "PLANNED_RULE_NOT_APPLICABLE"
    """A planned Rule is not applicable in the verification phase, so the reused
    plan can never be fully covered and no score would be comparable."""


class VerificationScopeError(ValueError):
    """Raised when the verification scope cannot be fixed to the source Assessment."""

    def __init__(self, message: str, *, code: VerificationRejectionCode) -> None:
        super().__init__(message)
        if not isinstance(code, VerificationRejectionCode):
            raise TypeError("code must be a VerificationRejectionCode")
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationSource:
    """The durable facts of the Assessment being verified.

    `model_profile_id` and `rubric_version` are facts of the source evaluation,
    not caller preferences: ADR-0020 §3 forbids re-evaluating with a different
    Profile or rubric because the delta would no longer be attributable.
    """

    assessment_id: str
    customer_id: str
    repository_id: str
    policy_profile_id: str
    policy_profile_version: str
    model_profile_id: str
    rubric_version: str
    phase: AssessmentPhase
    planned_coordinates: tuple[PlannedEvaluation, ...]

    def __post_init__(self) -> None:
        for name in (
            "assessment_id",
            "customer_id",
            "repository_id",
            "policy_profile_id",
            "policy_profile_version",
            "model_profile_id",
            "rubric_version",
        ):
            _require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        _require_planned_coordinates(self.planned_coordinates)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationAssessmentScope:
    """The verification Assessment plus the plan its evaluation must reuse.

    The Profile version, Model Profile, and rubric pin lives on the Assessment
    itself because it has to survive until the re-read (ADR-0020 §3). The plan is
    returned beside it because a reused plan cannot be derived by the Worker:
    derivation only knows the Rule set of the task that happens to run, while the
    comparison requires the source's exact set (ADR-0020 §5).
    """

    assessment: Assessment
    planned_coordinates: tuple[PlannedEvaluation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, Assessment):
            raise TypeError("assessment must be an Assessment")
        if self.assessment.phase is not AssessmentPhase.POST_DEPLOY_VERIFICATION:
            raise ValueError("verification scope requires a POST_DEPLOY_VERIFICATION assessment")
        _require_planned_coordinates(self.planned_coordinates)


def plan_verification_assessment(
    *,
    source: VerificationSource,
    context: PolicyContext,
    deployment_id: str,
    assessment_id: str,
    job_id: str,
) -> VerificationAssessmentScope:
    """Return the verification Assessment pinned to the source Assessment's scope.

    `context` must already be resolved with the source Profile version pin
    (`PolicyContextResolver.resolve(expected_profile_version=...)`) and with
    `phase=POST_DEPLOY_VERIFICATION`. It is passed in rather than resolved here so
    this boundary stays pure and so the caller's Rule allow-list is the one that
    the reused plan is checked against.

    `job_id` is the Deployment Job that owns the verification: one Deployment is
    one Job and its write-once `assessment_id` names this Assessment (ADR-0020 §7).
    A Job is therefore never created here.
    """
    if not isinstance(source, VerificationSource):
        raise TypeError("source must be a VerificationSource")
    if not isinstance(context, PolicyContext):
        raise TypeError("context must be a PolicyContext")
    for value, name in ((deployment_id, "deployment_id"), (assessment_id, "assessment_id")):
        _require_non_empty_string(value, name)
    _require_non_empty_string(job_id, "job_id")

    if context.policy_profile_id != source.policy_profile_id:
        raise VerificationScopeError(
            "verification context resolves a different policy profile than the source assessment",
            code=VerificationRejectionCode.POLICY_PROFILE_MISMATCH,
        )
    if context.policy_profile_version != source.policy_profile_version:
        raise VerificationScopeError(
            "policy profile version changed; evaluate a new initial assessment instead",
            code=VerificationRejectionCode.POLICY_PROFILE_VERSION_REPLACED,
        )
    if context.phase is not AssessmentPhase.POST_DEPLOY_VERIFICATION:
        raise VerificationScopeError(
            "verification context must be resolved for the post-deploy verification phase",
            code=VerificationRejectionCode.CONTEXT_PHASE_MISMATCH,
        )
    applicable_rule_ids = {rule.rule_id for rule in context.rules}
    planned_rule_ids = {coordinate.rule_id for coordinate in source.planned_coordinates}
    if not planned_rule_ids <= applicable_rule_ids:
        raise VerificationScopeError(
            "planned rule is not applicable in the post-deploy verification phase",
            code=VerificationRejectionCode.PLANNED_RULE_NOT_APPLICABLE,
        )

    assessment = Assessment(
        assessment_id=assessment_id,
        customer_id=source.customer_id,
        job_id=job_id,
        repository_id=source.repository_id,
        policy_profile_id=source.policy_profile_id,
        phase=AssessmentPhase.POST_DEPLOY_VERIFICATION,
        source_assessment_id=source.assessment_id,
        deployment_id=deployment_id,
        model_profile_id=source.model_profile_id,
        rubric_version=source.rubric_version,
        policy_profile_version=source.policy_profile_version,
    )
    return VerificationAssessmentScope(
        assessment=assessment,
        planned_coordinates=source.planned_coordinates,
    )


def _require_planned_coordinates(value: object) -> None:
    if not isinstance(value, tuple) or not value:
        raise ValueError("planned_coordinates must be a non-empty tuple")
    if not all(isinstance(coordinate, PlannedEvaluation) for coordinate in value):
        raise TypeError("planned_coordinates must contain PlannedEvaluation values")
    if len(set(value)) != len(value):
        raise ValueError("planned_coordinates must not contain duplicates")


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
