"""Policy and Golden Dataset contracts for the approved evaluation boundary."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string
from packages.contracts.assessments import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    ScoringMode,
)


class PolicySourceKind(StrEnum):
    INTERNAL_POLICY = "INTERNAL_POLICY"
    ISMS_P = "ISMS_P"


class RuleSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceReference:
    """A traceable locator within an approved policy source artifact."""

    source_id: str
    locator: str
    content_sha256: str

    def __post_init__(self) -> None:
        for name in ("source_id", "locator", "content_sha256"):
            require_non_empty_string(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "locator": self.locator,
            "content_sha256": self.content_sha256,
        }

    @property
    def evidence_reference(self) -> str:
        """Canonical policy evidence identifier: ``{source_id}#{locator}``."""
        return f"{self.source_id}#{self.locator}"


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySource:
    source_id: str
    kind: PolicySourceKind
    title: str
    version: str
    artifact_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        for name in ("source_id", "title", "version", "artifact_id", "content_sha256"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.kind, PolicySourceKind):
            raise TypeError("kind must be a PolicySourceKind")

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "title": self.title,
            "version": self.version,
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRule:
    rule_id: str
    version: str
    title: str
    severity: RuleSeverity
    applicable_phases: tuple[AssessmentPhase, ...]
    resource_types: tuple[str, ...]
    source_references: tuple[SourceReference, ...]

    def __post_init__(self) -> None:
        for name in ("rule_id", "version", "title"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.severity, RuleSeverity):
            raise TypeError("severity must be a RuleSeverity")
        if not self.applicable_phases:
            raise ValueError("applicable_phases must not be empty")
        if not self.resource_types:
            raise ValueError("resource_types must not be empty")
        if not self.source_references:
            raise ValueError("source_references must not be empty")
        for phase in self.applicable_phases:
            if not isinstance(phase, AssessmentPhase):
                raise TypeError("applicable_phases items must be AssessmentPhase values")
        for resource_type in self.resource_types:
            require_non_empty_string(resource_type, "resource_types item")
        for reference in self.source_references:
            if not isinstance(reference, SourceReference):
                raise TypeError("source_references items must be SourceReference values")

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "title": self.title,
            "severity": self.severity.value,
            "applicable_phases": [phase.value for phase in self.applicable_phases],
            "resource_types": list(self.resource_types),
            "source_references": [reference.to_dict() for reference in self.source_references],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRuleReference:
    """An immutable Profile reference to one exact version of a Policy Rule."""

    rule_id: str
    version: str

    def __post_init__(self) -> None:
        require_non_empty_string(self.rule_id, "rule_id")
        require_non_empty_string(self.version, "version")

    def to_dict(self) -> dict[str, str]:
        return {"rule_id": self.rule_id, "version": self.version}


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyProfile:
    policy_profile_id: str
    version: str
    rule_references: tuple[PolicyRuleReference, ...]

    def __post_init__(self) -> None:
        require_non_empty_string(self.policy_profile_id, "policy_profile_id")
        require_non_empty_string(self.version, "version")
        if not self.rule_references:
            raise ValueError("rule_references must not be empty")
        for reference in self.rule_references:
            if not isinstance(reference, PolicyRuleReference):
                raise TypeError("rule_references items must be PolicyRuleReference values")

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_profile_id": self.policy_profile_id,
            "version": self.version,
            "rule_references": [reference.to_dict() for reference in self.rule_references],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenDatasetCase:
    case_id: str
    phase: AssessmentPhase
    perspective: EvaluationPerspective
    rubric_version: str
    scoring_mode: ScoringMode
    resource_snapshot_artifact_id: str
    expected_status: EvaluationStatus
    expected_score_min: float
    expected_score_max: float
    expected_evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("case_id", "rubric_version", "resource_snapshot_artifact_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        if not isinstance(self.scoring_mode, ScoringMode):
            raise TypeError("scoring_mode must be a ScoringMode")
        if not isinstance(self.expected_status, EvaluationStatus):
            raise TypeError("expected_status must be an EvaluationStatus")
        for name in ("expected_score_min", "expected_score_max"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if self.expected_score_min > self.expected_score_max:
            raise ValueError("expected_score_min must not exceed expected_score_max")
        for reference in self.expected_evidence_references:
            require_non_empty_string(reference, "expected_evidence_references item")

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "phase": self.phase.value,
            "perspective": self.perspective.value,
            "rubric_version": self.rubric_version,
            "scoring_mode": self.scoring_mode.value,
            "resource_snapshot_artifact_id": self.resource_snapshot_artifact_id,
            "expected_status": self.expected_status.value,
            "expected_score_min": self.expected_score_min,
            "expected_score_max": self.expected_score_max,
            "expected_evidence_references": list(self.expected_evidence_references),
        }
