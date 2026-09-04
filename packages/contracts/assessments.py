"""Assessment phases and structured AI evaluation output contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from packages.contracts._validation import (
    require_non_empty_string,
    require_offset_aware_timestamp,
)


class AssessmentPhase(StrEnum):
    INITIAL = "INITIAL"
    DEPLOYMENT_READINESS = "DEPLOYMENT_READINESS"
    POST_DEPLOY_VERIFICATION = "POST_DEPLOY_VERIFICATION"


class EvaluationPerspective(StrEnum):
    """The source relationship represented by a Resource × Rule result.

    `MANUAL`은 additive다. 사람이 검토해야 할 조직 통제를 기존 IAC/AWS_ACTUAL/DRIFT 중 하나로
    표현하면, 그 결과가 "IaC를 읽고 내린 판단"처럼 보인다 — 근거가 무엇이었는지 결과에서
    복원할 수 없게 된다. 이 값을 더해도 기존 status·scoring·severity 계약은 바뀌지 않는다.
    """

    IAC = "IAC"
    AWS_ACTUAL = "AWS_ACTUAL"
    DRIFT = "DRIFT"
    MANUAL = "MANUAL"


class EvaluationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    EXECUTION_ERROR = "EXECUTION_ERROR"


class FindingResolution(StrEnum):
    """Deterministic before/after state for one evaluation coordinate."""

    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    REGRESSED = "REGRESSED"
    INDETERMINATE = "INDETERMINATE"
    NO_LONGER_APPLICABLE = "NO_LONGER_APPLICABLE"


class ComparisonIneligibilityReason(StrEnum):
    """Why a verification report must not present a numerical delta."""

    SOURCE_READINESS_UNAVAILABLE = "SOURCE_READINESS_UNAVAILABLE"
    VERIFICATION_READINESS_UNAVAILABLE = "VERIFICATION_READINESS_UNAVAILABLE"
    PLANNED_EVALUATIONS_MISMATCH = "PLANNED_EVALUATIONS_MISMATCH"
    MODEL_PROFILE_MISMATCH = "MODEL_PROFILE_MISMATCH"
    RUBRIC_VERSION_MISMATCH = "RUBRIC_VERSION_MISMATCH"


class ScoringMode(StrEnum):
    """Score policy selected by the evaluator's approved rubric."""

    CONTINUOUS = "CONTINUOUS"
    ANCHORED = "ANCHORED"


SCORE_ANCHORS = frozenset({0, 15, 30, 50, 70, 85, 100})


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentCoverage:
    """Completion against the immutable applicable evaluation plan."""

    planned_evaluations: int
    completed_evaluations: int

    def __post_init__(self) -> None:
        for field_name in ("planned_evaluations", "completed_evaluations"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
            if value < 0:
                raise ValueError(f"{field_name} must not be negative")
        if self.planned_evaluations == 0:
            raise ValueError("planned_evaluations must be greater than zero")
        if self.completed_evaluations > self.planned_evaluations:
            raise ValueError("completed_evaluations must not exceed planned_evaluations")

    @property
    def percentage(self) -> float:
        return round(self.completed_evaluations / self.planned_evaluations * 100, 2)

    def to_dict(self) -> dict[str, object]:
        return {
            "planned_evaluations": self.planned_evaluations,
            "completed_evaluations": self.completed_evaluations,
            "percentage": self.percentage,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationResult:
    """Validated Resource × Rule result produced by the AI evaluation boundary."""

    resource_id: str
    rule_id: str
    perspective: EvaluationPerspective
    status: EvaluationStatus
    severity: str
    score: float
    rationale: str
    evidence_references: tuple[str, ...]
    rule_version: str
    rubric_version: str
    model_profile_id: str
    scoring_mode: ScoringMode = ScoringMode.CONTINUOUS
    assessed_commit_sha: str | None = None
    evaluated_at: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "resource_id",
            "rule_id",
            "severity",
            "rationale",
            "rule_version",
            "rubric_version",
            "model_profile_id",
        ):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if not isinstance(self.status, EvaluationStatus):
            raise TypeError("status must be an EvaluationStatus")
        if not isinstance(self.scoring_mode, ScoringMode):
            raise TypeError("scoring_mode must be a ScoringMode")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("score must be a number")
        if not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if self.scoring_mode is ScoringMode.ANCHORED and self.score not in SCORE_ANCHORS:
            raise ValueError("anchored score must be one of the approved score anchors")
        if not isinstance(self.evidence_references, tuple):
            raise TypeError("evidence_references must be a tuple")
        for reference in self.evidence_references:
            require_non_empty_string(reference, "evidence_references item")
        _require_provenance(self.assessed_commit_sha, self.evaluated_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "rule_id": self.rule_id,
            "perspective": self.perspective.value,
            "status": self.status.value,
            "severity": self.severity,
            "score": self.score,
            "rationale": self.rationale,
            "evidence_references": list(self.evidence_references),
            "rule_version": self.rule_version,
            "rubric_version": self.rubric_version,
            "model_profile_id": self.model_profile_id,
            "scoring_mode": self.scoring_mode.value,
            "assessed_commit_sha": self.assessed_commit_sha,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """An actionable, immutable projection of one assessment evaluation."""

    finding_id: str
    resource_id: str
    rule_id: str
    rule_version: str
    perspective: EvaluationPerspective
    status: EvaluationStatus
    severity: str
    score: float
    rationale: str
    evidence_references: tuple[str, ...]
    assessed_commit_sha: str | None = None
    evaluated_at: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "finding_id",
            "resource_id",
            "rule_id",
            "rule_version",
            "severity",
            "rationale",
        ):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if self.status not in {
            EvaluationStatus.FAIL,
            EvaluationStatus.MANUAL_REVIEW,
            EvaluationStatus.INSUFFICIENT_EVIDENCE,
        }:
            raise ValueError("finding status must require follow-up")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("score must be a number")
        if not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if not isinstance(self.evidence_references, tuple):
            raise TypeError("evidence_references must be a tuple")
        for reference in self.evidence_references:
            require_non_empty_string(reference, "evidence_references item")
        _require_provenance(self.assessed_commit_sha, self.evaluated_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "resource_id": self.resource_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "perspective": self.perspective.value,
            "status": self.status.value,
            "severity": self.severity,
            "score": self.score,
            "rationale": self.rationale,
            "evidence_references": list(self.evidence_references),
            "assessed_commit_sha": self.assessed_commit_sha,
            "evaluated_at": self.evaluated_at,
        }

    @property
    def evaluated_at_utc(self) -> datetime:
        """The evaluation time as an offset-aware moment, for time-ordered comparisons.

        `evaluated_at`은 저장·전송을 위한 문자열이지만, 예외의 승인·만료와 순서를 비교하려면
        시각이어야 한다. 소비자가 각자 파싱하면 같은 값에 서로 다른 파싱 규칙이 생기므로
        `RemediationException.approved_at_utc`와 같은 방식으로 여기서 한 번만 정의한다.

        provenance가 없는 옛 record에는 비교할 시각 자체가 없다. `None`을 조용히 현재 시각
        같은 것으로 대체하면 "승인은 평가보다 앞서야 한다"는 규칙이 무너지므로 거부한다.
        """
        if self.evaluated_at is None:
            raise ValueError("finding has no evaluation provenance")
        return require_offset_aware_timestamp(self.evaluated_at, "evaluated_at")


def _require_provenance(assessed_commit_sha: str | None, evaluated_at: str | None) -> None:
    """Validate present provenance; absent values represent legacy records only.

    Remediation consumers must reject absent provenance rather than infer it from
    a current snapshot.  Keeping it representable allows those legacy records to
    be read and reported without accidentally reopening an automated action.
    """
    if (assessed_commit_sha is None) != (evaluated_at is None):
        raise ValueError("assessed_commit_sha and evaluated_at must be provided together")
    if assessed_commit_sha is None:
        return
    require_non_empty_string(assessed_commit_sha, "assessed_commit_sha")
    # 파싱 규칙은 `Finding.evaluated_at_utc`가 쓰는 것과 같아야 한다. 여기서만 다르게 받아들이면
    # 생성은 통과하고 비교 시점에 실패하는 값이 만들어진다.
    require_offset_aware_timestamp(evaluated_at, "evaluated_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadinessScore:
    """Deterministic Assessment-level score over completed applicable results."""

    score: float
    evaluated_evaluations: int

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise TypeError("score must be a number")
        if not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if isinstance(self.evaluated_evaluations, bool) or not isinstance(
            self.evaluated_evaluations, int
        ):
            raise TypeError("evaluated_evaluations must be an integer")
        if self.evaluated_evaluations <= 0:
            raise ValueError("evaluated_evaluations must be greater than zero")

    def to_dict(self) -> dict[str, object]:
        return {"score": self.score, "evaluated_evaluations": self.evaluated_evaluations}


@dataclass(frozen=True, slots=True, kw_only=True)
class SegmentReadinessScore:
    """One policy origin's readiness inside a Profile that spans several.

    한 Profile은 사내 정책 문서와 ISMS-P 기준선을 함께 담을 수 있다. 그 둘의 준비도를 하나의
    숫자로 합치면 그 숫자는 어느 쪽에 대한 답도 아니다 — 사내 기준 미달과 인증 기준 미달은 서로
    다른 조치를 부르고, 한쪽이 다른 쪽을 가린다. 그래서 점수는 원본별로 따로 낸다.

    `kind`는 `PolicySourceKind`의 값 문자열이다. enum 자체를 쓰지 않는 이유는 import 방향
    하나뿐이다 — `packages.contracts.policy`가 이 모듈을 import하므로 반대로 가져올 수 없다.

    `score`가 `None`인 것은 "이 원본의 계획이 아직 다 끝나지 않았다"이지 0점이 아니다.
    """

    kind: str
    score: ReadinessScore | None

    def __post_init__(self) -> None:
        require_non_empty_string(self.kind, "kind")
        if self.score is not None and not isinstance(self.score, ReadinessScore):
            raise TypeError("score must be a ReadinessScore or None")

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "score": None if self.score is None else self.score.to_dict()}


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedEvaluation:
    """One planned applicable coordinate, fixed by the server before evaluation.

    The Assessment plan is this set, not a count. `rule_version` is deliberately
    absent: a coordinate whose Rule version changed must still pair up across two
    Assessments so the comparison can report it as `INDETERMINATE` (ADR-0020 §4).
    """

    resource_id: str
    rule_id: str
    perspective: EvaluationPerspective

    def __post_init__(self) -> None:
        for name in ("resource_id", "rule_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "rule_id": self.rule_id,
            "perspective": self.perspective.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingResolutionResult:
    """One stable Resource × Rule × Perspective comparison result."""

    resource_id: str
    rule_id: str
    rule_version: str
    perspective: EvaluationPerspective
    resolution: FindingResolution

    def __post_init__(self) -> None:
        for name in ("resource_id", "rule_id", "rule_version"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if not isinstance(self.resolution, FindingResolution):
            raise TypeError("resolution must be a FindingResolution")

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "perspective": self.perspective.value,
            "resolution": self.resolution.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentComparison:
    """Read-only Post-Deploy Verification projection over immutable Assessments."""

    source_assessment_id: str
    verification_assessment_id: str
    deployment_id: str
    comparable: bool
    ineligibility_reasons: tuple[ComparisonIneligibilityReason, ...]
    source_coverage: AssessmentCoverage
    verification_coverage: AssessmentCoverage
    source_readiness_score: ReadinessScore | None
    verification_readiness_score: ReadinessScore | None
    readiness_score_delta: float | None
    finding_resolutions: tuple[FindingResolutionResult, ...]

    def __post_init__(self) -> None:
        for name in ("source_assessment_id", "verification_assessment_id", "deployment_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.comparable, bool):
            raise TypeError("comparable must be a boolean")
        if not isinstance(self.ineligibility_reasons, tuple) or not all(
            isinstance(reason, ComparisonIneligibilityReason)
            for reason in self.ineligibility_reasons
        ):
            raise TypeError("ineligibility_reasons must be a tuple of reasons")
        if self.comparable != (not self.ineligibility_reasons):
            raise ValueError("comparable must agree with ineligibility_reasons")
        if not isinstance(self.source_coverage, AssessmentCoverage) or not isinstance(
            self.verification_coverage, AssessmentCoverage
        ):
            raise TypeError("coverage values must be AssessmentCoverage")
        for name in ("source_readiness_score", "verification_readiness_score"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, ReadinessScore):
                raise TypeError(f"{name} must be a ReadinessScore or None")
        if self.comparable:
            if self.readiness_score_delta is None:
                raise ValueError("comparable comparison requires readiness_score_delta")
            if self.source_readiness_score is None or self.verification_readiness_score is None:
                raise ValueError("comparable comparison requires both readiness scores")
        elif self.readiness_score_delta is not None:
            raise ValueError("incomparable comparison must not contain readiness_score_delta")
        if self.readiness_score_delta is not None and (
            isinstance(self.readiness_score_delta, bool)
            or not isinstance(self.readiness_score_delta, (int, float))
        ):
            raise TypeError("readiness_score_delta must be a number or None")
        if not isinstance(self.finding_resolutions, tuple) or not all(
            isinstance(value, FindingResolutionResult) for value in self.finding_resolutions
        ):
            raise TypeError("finding_resolutions must be a tuple of FindingResolutionResult")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_assessment_id": self.source_assessment_id,
            "verification_assessment_id": self.verification_assessment_id,
            "deployment_id": self.deployment_id,
            "comparable": self.comparable,
            "ineligibility_reasons": [reason.value for reason in self.ineligibility_reasons],
            "source_coverage": self.source_coverage.to_dict(),
            "verification_coverage": self.verification_coverage.to_dict(),
            "source_readiness_score": (
                self.source_readiness_score.to_dict()
                if self.source_readiness_score is not None
                else None
            ),
            "verification_readiness_score": (
                self.verification_readiness_score.to_dict()
                if self.verification_readiness_score is not None
                else None
            ),
            "readiness_score_delta": self.readiness_score_delta,
            "finding_resolutions": [value.to_dict() for value in self.finding_resolutions],
        }
