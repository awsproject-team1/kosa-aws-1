"""Policy and Golden Dataset contracts for the approved evaluation boundary."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import (
    require_non_empty_string,
    require_optional_non_empty_string,
)
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


class RuleEvaluationType(StrEnum):
    """How an approved Rule is executed at assessment time.

    `EvaluationPerspective`와 다른 질문에 답한다. Perspective는 "이 결과가 어떤 자료를 근거로
    나왔는가"이고, `RuleEvaluationType`은 "이 Rule을 어떤 실행 경로로 평가하는가"다. Runtime의
    execution planner가 이 값 하나로 Perspective 집합을 결정한다.
    """

    IAC = "IAC"
    AWS = "AWS"
    HYBRID = "HYBRID"
    MANUAL = "MANUAL"


# 자동 평가가 가능한 실행 유형. MANUAL은 사람 검토로만 종결된다.
AUTOMATED_EVALUATION_TYPES: frozenset[RuleEvaluationType] = frozenset(
    {RuleEvaluationType.IAC, RuleEvaluationType.AWS, RuleEvaluationType.HYBRID}
)

# 자유 텍스트 실행 의미 필드의 상한. Rule item은 DynamoDB에 저장되고 prompt로도 들어가므로
# 상한이 없으면 한 Rule이 item 크기와 prompt 예산을 모두 삼킬 수 있다.
MAX_APPLICABILITY_SEMANTICS_LENGTH = 2000
MAX_EVALUATION_RUBRIC_LENGTH = 4000
MAX_SEVERITY_GUIDANCE_LENGTH = 1000
MAX_EXCEPTION_SEMANTICS_LENGTH = 2000
MAX_COMPENSATING_CONTROL_SEMANTICS_LENGTH = 2000
MAX_EVIDENCE_CAPABILITIES = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceReference:
    """A traceable locator within one exact version of an approved policy source.

    `source_version`은 Rule과 Control을 특정 Policy Source version에 고정한다. 원문이 개정되면
    같은 locator라도 다른 내용을 가리키므로, 버전을 함께 고정해야 Evidence가 재현 가능하다.
    """

    source_id: str
    source_version: str
    locator: str
    content_sha256: str

    def __post_init__(self) -> None:
        for name in ("source_id", "source_version", "locator", "content_sha256"):
            require_non_empty_string(getattr(self, name), name)

    @property
    def evidence_reference(self) -> str:
        """Canonical evidence string: `{source_id}@{source_version}#{locator}`.

        평가 결과의 Evidence는 이 형식을 사용한다. locator만으로는 어떤 Source의 어느 version을
        인용했는지 복원할 수 없다.
        """
        return f"{self.source_id}@{self.source_version}#{self.locator}"

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "locator": self.locator,
            "content_sha256": self.content_sha256,
        }


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


def _require_unique_non_empty_strings(value: object, field_name: str) -> None:
    """Require a tuple of distinct, non-empty capability keys.

    중복이나 빈 문자열을 통과시키면 evidence 집합의 크기가 실제 요구 항목 수와 달라진다.
    Runtime은 그 크기로 pre-flight 판정을 하므로 조용히 어긋나면 안 된다.
    """
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if len(value) > MAX_EVIDENCE_CAPABILITIES:
        raise ValueError(f"{field_name} must carry at most {MAX_EVIDENCE_CAPABILITIES} entries")
    seen: set[str] = set()
    for entry in value:
        require_non_empty_string(entry, f"{field_name} item")
        if entry in seen:
            raise ValueError(f"{field_name} must not repeat {entry!r}")
        seen.add(entry)


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRule:
    """An approved evaluation unit bound to one exact policy source version.

    실행 의미 필드(`control_key` 이하)는 additive다. `evaluation_type is None`인 Rule은 authoring
    파이프라인 이전에 커밋된 legacy fixture Rule이며, Runtime은 그 Rule을 기존 3 Perspective로
    계속 평가한다. legacy Rule이 신규 필드를 **일부만** 갖는 상태는 금지한다 — 절반만 채워진
    실행 의미는 authoring이 만든 Rule과 손으로 쓴 Rule 중 어느 계약을 따르는지 알 수 없다.
    """

    rule_id: str
    version: str
    title: str
    severity: RuleSeverity
    applicable_phases: tuple[AssessmentPhase, ...]
    resource_types: tuple[str, ...]
    source_references: tuple[SourceReference, ...]
    control_key: str | None = None
    control_catalog_version: str | None = None
    evaluation_type: RuleEvaluationType | None = None
    applicability_semantics: str | None = None
    required_evidence: tuple[str, ...] = ()
    optional_evidence: tuple[str, ...] = ()
    evaluation_rubric: str | None = None
    severity_guidance: str | None = None
    exception_semantics: str | None = None
    compensating_control_semantics: str | None = None

    # `evaluation_type is None`인 legacy Rule이 가져서는 안 되는 실행 의미 필드.
    _EXECUTION_SEMANTICS_FIELDS = (
        "control_key",
        "control_catalog_version",
        "applicability_semantics",
        "required_evidence",
        "optional_evidence",
        "evaluation_rubric",
        "severity_guidance",
        "exception_semantics",
        "compensating_control_semantics",
    )

    _TEXT_FIELD_LIMITS = (
        ("applicability_semantics", MAX_APPLICABILITY_SEMANTICS_LENGTH),
        ("evaluation_rubric", MAX_EVALUATION_RUBRIC_LENGTH),
        ("severity_guidance", MAX_SEVERITY_GUIDANCE_LENGTH),
        ("exception_semantics", MAX_EXCEPTION_SEMANTICS_LENGTH),
        ("compensating_control_semantics", MAX_COMPENSATING_CONTROL_SEMANTICS_LENGTH),
    )

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
        self._require_valid_execution_semantics()

    def _require_valid_execution_semantics(self) -> None:
        for name in ("control_key", "control_catalog_version"):
            require_optional_non_empty_string(getattr(self, name), name)
        if self.evaluation_type is not None and not isinstance(
            self.evaluation_type, RuleEvaluationType
        ):
            raise TypeError("evaluation_type must be a RuleEvaluationType")
        for name, limit in self._TEXT_FIELD_LIMITS:
            value = getattr(self, name)
            require_optional_non_empty_string(value, name)
            if value is not None and len(value) > limit:
                raise ValueError(f"{name} must be at most {limit} characters")
        for name in ("required_evidence", "optional_evidence"):
            _require_unique_non_empty_strings(getattr(self, name), name)

        if self.evaluation_type is None:
            populated = [name for name in self._EXECUTION_SEMANTICS_FIELDS if getattr(self, name)]
            if populated:
                raise ValueError(
                    "a rule without an evaluation_type must not carry execution semantics: "
                    + ", ".join(sorted(populated))
                )
            return

        for name in ("control_key", "control_catalog_version"):
            if getattr(self, name) is None:
                raise ValueError(f"an executable rule must carry {name}")

        if self.evaluation_type is RuleEvaluationType.MANUAL:
            if self.required_evidence or self.optional_evidence:
                raise ValueError("a MANUAL rule must not carry evidence capabilities")
            return

        if self.evaluation_rubric is None:
            raise ValueError("an automated rule must carry an evaluation_rubric")
        if not self.required_evidence:
            raise ValueError("an automated rule must carry at least one required evidence")
        overlap = sorted(set(self.required_evidence) & set(self.optional_evidence))
        if overlap:
            raise ValueError(
                "evidence capability must not be both required and optional: " + ", ".join(overlap)
            )

    @property
    def is_legacy(self) -> bool:
        """Whether this Rule predates the authoring pipeline execution semantics."""
        return self.evaluation_type is None

    def to_dict(self) -> dict[str, object]:
        """Serialize the Rule, omitting execution semantics the Rule does not carry.

        비어 있는 신규 필드를 `null`로 내보내지 않는다. legacy Rule의 직렬화 결과가 필드 추가
        전과 완전히 같아야, 이미 저장된 DynamoDB item·커밋된 fixture와 재직렬화 결과를 그대로
        대조할 수 있다(멱등 write의 "같은 내용" 판정이 이 동등성에 걸려 있다).
        """
        payload: dict[str, object] = {
            "rule_id": self.rule_id,
            "version": self.version,
            "title": self.title,
            "severity": self.severity.value,
            "applicable_phases": [phase.value for phase in self.applicable_phases],
            "resource_types": list(self.resource_types),
            "source_references": [reference.to_dict() for reference in self.source_references],
        }
        if self.evaluation_type is not None:
            payload["evaluation_type"] = self.evaluation_type.value
        for name in (
            "control_key",
            "control_catalog_version",
            *(field for field, _ in self._TEXT_FIELD_LIMITS),
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        for name in ("required_evidence", "optional_evidence"):
            value = getattr(self, name)
            if value:
                payload[name] = list(value)
        return payload


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
class PolicyControl:
    """A policy source control and the versioned Rules that implement it.

    Control은 Rule보다 상위의 정책 통제 항목이다. Coverage는 이 매핑을 통해 "어떤 통제가 어떤
    Rule로 평가됐는지"로 설명된다. Rule의 `resource_types`가 Control을 Resource 유형에 전개한다.
    """

    control_id: str
    title: str
    source_reference: SourceReference
    rule_references: tuple[PolicyRuleReference, ...]

    def __post_init__(self) -> None:
        for name in ("control_id", "title"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.source_reference, SourceReference):
            raise TypeError("source_reference must be a SourceReference")
        if not self.rule_references:
            raise ValueError("rule_references must not be empty")
        for reference in self.rule_references:
            if not isinstance(reference, PolicyRuleReference):
                raise TypeError("rule_references items must be PolicyRuleReference values")

    def to_dict(self) -> dict[str, object]:
        return {
            "control_id": self.control_id,
            "title": self.title,
            "source_reference": self.source_reference.to_dict(),
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
