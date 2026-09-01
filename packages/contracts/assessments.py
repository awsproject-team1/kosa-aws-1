"""Assessment phases and structured AI evaluation output contracts."""

from dataclasses import dataclass
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
        }


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
