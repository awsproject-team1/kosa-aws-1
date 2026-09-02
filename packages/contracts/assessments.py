"""Assessment phases and structured AI evaluation output contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string


class AssessmentPhase(StrEnum):
    INITIAL = "INITIAL"
    DEPLOYMENT_READINESS = "DEPLOYMENT_READINESS"
    POST_DEPLOY_VERIFICATION = "POST_DEPLOY_VERIFICATION"


class EvaluationPerspective(StrEnum):
    """The source relationship represented by a Resource × Rule result."""

    IAC = "IAC"
    AWS_ACTUAL = "AWS_ACTUAL"
    DRIFT = "DRIFT"


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
    if not isinstance(evaluated_at, str) or not evaluated_at.strip():
        raise ValueError("evaluated_at must be an offset-aware ISO-8601 timestamp")
    parsed = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("evaluated_at must be an offset-aware ISO-8601 timestamp")


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
