"""Durable selector state for an Assessment workflow."""

from dataclasses import dataclass

from packages.contracts import AssessmentPhase

#: Post-Deploy Verification에만 붙는 pin. `policy_profile_version`은 여기 없다 — 그 값은
#: **모든 phase**가 갖는다. Initial이 Profile 판본을 고정하지 않으면, 실행 도중 새 Profile이
#: 게시됐을 때 같은 Assessment의 앞뒤 평가가 다른 Rule 집합을 쓰게 된다.
_VERIFICATION_ONLY_PINS = ("model_profile_id", "rubric_version")


@dataclass(frozen=True, slots=True, kw_only=True)
class Assessment:
    """Assessment selectors and immutable phase provenance persisted before dispatch."""

    assessment_id: str
    customer_id: str
    job_id: str
    repository_id: str
    policy_profile_id: str
    #: 이 Assessment가 사용할 Profile 판본. 생성 시점의 current pointer를 읽어 고정한다.
    #: Runtime은 latest pointer를 따라가지 않고 이 값을 직접 조회한다.
    policy_profile_version: str
    phase: AssessmentPhase = AssessmentPhase.INITIAL
    source_assessment_id: str | None = None
    deployment_id: str | None = None
    # The source Assessment's evaluation scope, pinned only for a verification.
    # ADR-0020 §2·§3 require a verification to reuse the Profile version, Model
    # Profile, and rubric it verifies, so the pin is durable rather than resolved
    # again at evaluation time: a Profile replaced between apply and re-read would
    # otherwise silently produce an incomparable Assessment. An Initial Assessment
    # carries no pin because its Model Profile is chosen by the approved Worker
    # configuration, not by the creation boundary.
    model_profile_id: str | None = None
    rubric_version: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "assessment_id",
            "customer_id",
            "job_id",
            "repository_id",
            "policy_profile_id",
            "policy_profile_version",
        ):
            _require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        for name in ("source_assessment_id", "deployment_id", *_VERIFICATION_ONLY_PINS):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_string(value, name)
        has_source = self.source_assessment_id is not None
        has_deployment = self.deployment_id is not None
        pins = tuple(getattr(self, name) for name in _VERIFICATION_ONLY_PINS)
        if self.phase is AssessmentPhase.POST_DEPLOY_VERIFICATION:
            if not has_source or not has_deployment:
                raise ValueError(
                    "POST_DEPLOY_VERIFICATION requires source_assessment_id and deployment_id"
                )
            if self.source_assessment_id == self.assessment_id:
                raise ValueError("verification assessment must differ from source assessment")
            if any(pin is None for pin in pins):
                raise ValueError(
                    f"POST_DEPLOY_VERIFICATION requires the source {', '.join(_VERIFICATION_ONLY_PINS)}"
                )
        else:
            if has_source or has_deployment:
                raise ValueError(
                    "source_assessment_id and deployment_id are only valid for "
                    "post-deploy verification"
                )
            if any(pin is not None for pin in pins):
                raise ValueError(
                    f"{', '.join(_VERIFICATION_ONLY_PINS)} are only valid for post-deploy verification"
                )


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
