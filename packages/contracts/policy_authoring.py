"""Policy authoring contracts: what the extractor may say and what the catalog allows.

이 모듈은 **authoring 단계**의 어휘만 정의한다. Runtime 평가 결과(`EvaluationStatus`,
`score`, `severity`)는 여기 없다. 두 어휘를 한 모듈에 두면 "이 Requirement를 어떤 Rule로
만들 수 있는가"와 "그 Rule을 평가한 결과가 무엇인가"가 같은 값으로 표현되기 시작한다.
`CandidateClassification`과 `EvaluationStatus` 사이에는 alias도 변환 함수도 만들지 않는다.

**텍스트 경계.** `ExtractedRequirement.requirement`는 모델이 쓴 재진술이지 정규화 문서의
원문이 아니다. 원문 문장은 `ExtractionUnit`(`apps/backend/policy/authoring/artifact_reader.py`)
안에만 존재하고 그 값은 직렬화를 제공하지 않는다. 리뷰어에게 보여줄 문장과 보호해야 할 원문을
서로 다른 타입으로 분리해, 원문이 DynamoDB·API·log로 나가는 경로를 구조적으로 막는다.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256

from packages.contracts._validation import (
    require_non_empty_string,
    require_offset_aware_timestamp,
    require_optional_non_empty_string,
)
from packages.contracts.assessments import EvaluationPerspective
from packages.contracts.policy import (
    MAX_APPLICABILITY_SEMANTICS_LENGTH,
    MAX_COMPENSATING_CONTROL_SEMANTICS_LENGTH,
    MAX_EVALUATION_RUBRIC_LENGTH,
    MAX_EVIDENCE_CAPABILITIES,
    MAX_EXCEPTION_SEMANTICS_LENGTH,
    MAX_SEVERITY_GUIDANCE_LENGTH,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)
from packages.contracts.policy_approval import RuleCandidate
from packages.contracts.policy_ingestion import NormalizedPolicyDocument

# LLM structured output의 schema 판. 이 값이 바뀌면 같은 source version이라도 다른 추출로 본다.
CANDIDATE_SCHEMA_VERSION = "policy-candidate/2026-09-03"

# 모델이 쓴 재진술 텍스트의 상한. 원문을 그대로 복사해 넣는 경로를 예산으로도 제한한다.
MAX_REQUIREMENT_LENGTH = 2000
MAX_REQUIREMENT_SUMMARY_LENGTH = 300
MAX_MAPPING_REASON_LENGTH = 1000
MAX_LOCATORS_PER_REQUIREMENT = 20

# `ExtractedRequirement`가 절대 정의하지 않는 필드. LLM이 평가 결과를 쓰는 자리를 만들지 않는다.
FORBIDDEN_EXTRACTION_FIELDS: frozenset[str] = frozenset(
    {"judgment", "severity", "score", "source_score", "anchor"}
)


class CandidateClassification(StrEnum):
    """How a extracted policy Requirement can be turned into a Rule — not its evaluation result.

    `EvaluationStatus`와 다른 질문에 답한다. 이 값은 authoring 시점에 정해지고, 승인된 Rule을
    특정 대상에 평가한 결과는 Runtime의 `EvaluationStatus`가 따로 말한다.
    """

    AUTOMATABLE = "AUTOMATABLE"
    MANUAL = "MANUAL"
    UNSUPPORTED = "UNSUPPORTED"


class ControlAutomationSupport(StrEnum):
    """Whether the product can currently evaluate a known Governance Control.

    Catalog에 존재한다는 것과 지금 자동으로 평가할 수 있다는 것은 다른 의미다. 그 둘을 한 값으로
    표현하면, 실행 경로가 없는 Control이 자동화 가능으로 노출된다.
    """

    AVAILABLE = "AVAILABLE"
    KNOWN_UNSUPPORTED = "KNOWN_UNSUPPORTED"
    MANUAL = "MANUAL"


class CandidateRejectionCode(StrEnum):
    """Why one extracted Requirement could not become an approvable Rule.

    자유 문장이 아니라 열거값이다. 거부 사유가 원문 문장을 인용해 log나 API로 새지 않는다.
    Artifact 자체의 무결성 실패(`CONTENT_DIGEST_MISMATCH` 등)는 여기 없다 — 그것은 후보 하나의
    문제가 아니라 추출 전체를 중단시키는 `ArtifactReadFailureCode`다.
    """

    UNKNOWN_LOCATOR = "UNKNOWN_LOCATOR"
    UNKNOWN_CONTROL_KEY = "UNKNOWN_CONTROL_KEY"
    UNSUPPORTED_RESOURCE_TYPE = "UNSUPPORTED_RESOURCE_TYPE"
    UNSUPPORTED_EVALUATION_TYPE = "UNSUPPORTED_EVALUATION_TYPE"
    EVIDENCE_CAPABILITY_NOT_AVAILABLE = "EVIDENCE_CAPABILITY_NOT_AVAILABLE"
    MISSING_REQUIRED_EVIDENCE = "MISSING_REQUIRED_EVIDENCE"
    MANUAL_RULE_WITH_EVIDENCE = "MANUAL_RULE_WITH_EVIDENCE"
    CLASSIFICATION_MAPPING_CONFLICT = "CLASSIFICATION_MAPPING_CONFLICT"


class ArtifactReadFailureCode(StrEnum):
    """Why the normalized artifact could not be trusted as extraction input.

    하나라도 발생하면 추출 전체를 중단한다. 후보 하나를 버리는 문제가 아니라, 읽은 텍스트가
    승인된 문서의 그 판본이 맞는지를 더 이상 보장할 수 없다는 뜻이기 때문이다.
    """

    SOURCE_NOT_READY = "SOURCE_NOT_READY"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_TOO_LARGE = "ARTIFACT_TOO_LARGE"
    ARTIFACT_SCHEMA_INVALID = "ARTIFACT_SCHEMA_INVALID"
    CONTENT_DIGEST_MISMATCH = "CONTENT_DIGEST_MISMATCH"
    SOURCE_VERSION_MISMATCH = "SOURCE_VERSION_MISMATCH"
    UNIT_SET_MISMATCH = "UNIT_SET_MISMATCH"
    UNIT_DIGEST_MISMATCH = "UNIT_DIGEST_MISMATCH"


def _require_unique_strings(
    value: object, field_name: str, *, limit: int, allow_empty: bool = True
) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if not value and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{field_name} must carry at most {limit} entries")
    seen: set[str] = set()
    for entry in value:
        require_non_empty_string(entry, f"{field_name} item")
        if entry in seen:
            raise ValueError(f"{field_name} must not repeat {entry!r}")
        seen.add(entry)


def _require_length(value: str | None, field_name: str, limit: int) -> None:
    require_optional_non_empty_string(value, field_name)
    if value is not None and len(value) > limit:
        raise ValueError(f"{field_name} must be at most {limit} characters")


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceCapabilityBinding:
    """One piece of evidence the product can actually obtain for a Control.

    **AWS와 IaC는 대칭이 아니다.** AWS Actual adapter는 구조화된 projected document를 돌려주므로
    `document_paths`로 field 존재 여부를 deterministic하게 검사할 수 있다. IaC evaluator는 raw
    HCL 텍스트를 받고 Evidence locator는 파일 경로 단위이므로, Terraform hint는 prompt 경계와
    리뷰 화면 설명에만 쓰는 non-authoritative 값이다. 두 값을 같은 필드에 담으면 hint가
    attribute-level 증거로 오해되어, HCL을 파싱하지도 않은 채 자동 판정 근거가 된다.
    """

    capability_key: str
    perspective: EvaluationPerspective
    resource_type: str
    # AWS_ACTUAL 전용 authoritative binding — projected document의 실제 경로.
    document_paths: tuple[str, ...] = ()
    # IAC 전용 non-authoritative hint — prompt 경계와 화면 설명 용도.
    terraform_resource_types: tuple[str, ...] = ()
    terraform_attribute_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty_string(self.capability_key, "capability_key")
        require_non_empty_string(self.resource_type, "resource_type")
        if not isinstance(self.perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        for name in ("document_paths", "terraform_resource_types", "terraform_attribute_names"):
            _require_unique_strings(getattr(self, name), name, limit=MAX_EVIDENCE_CAPABILITIES)

        if self.perspective is EvaluationPerspective.AWS_ACTUAL:
            if not self.document_paths:
                raise ValueError("an AWS_ACTUAL capability must declare document_paths")
            if self.terraform_resource_types or self.terraform_attribute_names:
                raise ValueError("an AWS_ACTUAL capability must not declare Terraform hints")
            return
        if self.perspective is EvaluationPerspective.IAC:
            if self.document_paths:
                raise ValueError(
                    "an IAC capability must not declare document_paths — "
                    "IaC evidence is file-scoped, not attribute-scoped"
                )
            return
        raise ValueError("an evidence capability must bind to IAC or AWS_ACTUAL")

    @property
    def is_authoritative(self) -> bool:
        """Whether Runtime may hard-gate on this binding before calling the model."""
        return self.perspective is EvaluationPerspective.AWS_ACTUAL

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_key": self.capability_key,
            "perspective": self.perspective.value,
            "resource_type": self.resource_type,
            "document_paths": list(self.document_paths),
            "terraform_resource_types": list(self.terraform_resource_types),
            "terraform_attribute_names": list(self.terraform_attribute_names),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceControl:
    """One Governance Control the product knows about, and how far it can evaluate it.

    Catalog는 정책 문서의 내용을 저장하는 곳이 아니다. 정책 문서와 AI가 실제로 사용할 수 있는
    평가 기능 사이의 **경계**를 정의한다.
    """

    control_key: str
    title: str
    description: str
    automation_support: ControlAutomationSupport
    supported_resource_types: tuple[str, ...] = ()
    supported_evaluation_types: tuple[RuleEvaluationType, ...] = ()
    available_evidence_capabilities: tuple[EvidenceCapabilityBinding, ...] = ()
    allowed_tool_bindings: tuple[str, ...] = ()
    baseline_required_evidence: tuple[str, ...] = ()
    baseline_optional_evidence: tuple[str, ...] = ()
    severity_guidance: str
    default_severity: RuleSeverity

    def __post_init__(self) -> None:
        for name in ("control_key", "title", "description"):
            require_non_empty_string(getattr(self, name), name)
        _require_length(self.severity_guidance, "severity_guidance", MAX_SEVERITY_GUIDANCE_LENGTH)
        if not isinstance(self.automation_support, ControlAutomationSupport):
            raise TypeError("automation_support must be a ControlAutomationSupport")
        if not isinstance(self.default_severity, RuleSeverity):
            raise TypeError("default_severity must be a RuleSeverity")
        for name in (
            "supported_resource_types",
            "allowed_tool_bindings",
            "baseline_required_evidence",
            "baseline_optional_evidence",
        ):
            _require_unique_strings(getattr(self, name), name, limit=MAX_EVIDENCE_CAPABILITIES)
        for evaluation_type in self.supported_evaluation_types:
            if not isinstance(evaluation_type, RuleEvaluationType):
                raise TypeError("supported_evaluation_types items must be RuleEvaluationType")
        if len(set(self.supported_evaluation_types)) != len(self.supported_evaluation_types):
            raise ValueError("supported_evaluation_types must not repeat a value")

        self._require_unique_capabilities()
        if self.automation_support is ControlAutomationSupport.MANUAL:
            self._require_manual_shape()
        elif self.automation_support is ControlAutomationSupport.KNOWN_UNSUPPORTED:
            self._require_known_unsupported_shape()
        else:
            self._require_available_shape()

    def _require_unique_capabilities(self) -> None:
        seen: set[str] = set()
        for binding in self.available_evidence_capabilities:
            if not isinstance(binding, EvidenceCapabilityBinding):
                raise TypeError(
                    "available_evidence_capabilities items must be EvidenceCapabilityBinding"
                )
            if binding.capability_key in seen:
                raise ValueError(f"duplicate evidence capability {binding.capability_key!r}")
            seen.add(binding.capability_key)
            if binding.resource_type not in self.supported_resource_types:
                raise ValueError(
                    f"capability {binding.capability_key!r} binds an unsupported resource type"
                )
            if binding.perspective is EvaluationPerspective.AWS_ACTUAL and not self._supports_aws():
                raise ValueError(
                    f"capability {binding.capability_key!r} is AWS_ACTUAL but the control "
                    "does not support an AWS evaluation type"
                )
            if binding.perspective is EvaluationPerspective.IAC and not self._supports_iac():
                raise ValueError(
                    f"capability {binding.capability_key!r} is IAC but the control "
                    "does not support an IaC evaluation type"
                )

    def _supports_aws(self) -> bool:
        return bool(
            {RuleEvaluationType.AWS, RuleEvaluationType.HYBRID}
            & set(self.supported_evaluation_types)
        )

    def _supports_iac(self) -> bool:
        return bool(
            {RuleEvaluationType.IAC, RuleEvaluationType.HYBRID}
            & set(self.supported_evaluation_types)
        )

    def _require_manual_shape(self) -> None:
        if self.supported_evaluation_types != (RuleEvaluationType.MANUAL,):
            raise ValueError("a MANUAL control must support exactly the MANUAL evaluation type")
        if (
            self.available_evidence_capabilities
            or self.allowed_tool_bindings
            or self.baseline_required_evidence
            or self.baseline_optional_evidence
        ):
            raise ValueError("a MANUAL control must not declare evidence or tool bindings")

    def _require_known_unsupported_shape(self) -> None:
        """알려졌지만 지금 실행할 수 없는 Control은 실행 가능한 것을 하나도 선언하지 않는다.

        capability나 evaluation type을 선언해 두면, Catalog를 읽는 쪽에서 "지원되는 Control"과
        구별할 방법이 없다. 존재를 기록하는 것과 실행 경로를 여는 것을 분리한다.
        """
        if self.supported_evaluation_types:
            raise ValueError(
                "a KNOWN_UNSUPPORTED control must not declare supported evaluation types"
            )
        if (
            self.available_evidence_capabilities
            or self.allowed_tool_bindings
            or self.baseline_required_evidence
            or self.baseline_optional_evidence
        ):
            raise ValueError(
                "a KNOWN_UNSUPPORTED control must not declare evidence or tool bindings"
            )

    def _require_available_shape(self) -> None:
        if RuleEvaluationType.MANUAL in self.supported_evaluation_types:
            raise ValueError("an AVAILABLE control must not support the MANUAL evaluation type")
        if not self.supported_evaluation_types:
            raise ValueError("an AVAILABLE control must support at least one evaluation type")
        if not self.supported_resource_types:
            raise ValueError("an AVAILABLE control must support at least one resource type")
        if not self.available_evidence_capabilities:
            raise ValueError("an AVAILABLE control must declare at least one evidence capability")
        if not self.baseline_required_evidence:
            raise ValueError("an AVAILABLE control must declare baseline required evidence")
        known = {binding.capability_key for binding in self.available_evidence_capabilities}
        baseline = set(self.baseline_required_evidence) | set(self.baseline_optional_evidence)
        unknown = sorted(baseline - known)
        if unknown:
            raise ValueError(
                "baseline evidence must be declared capabilities: " + ", ".join(unknown)
            )
        overlap = sorted(
            set(self.baseline_required_evidence) & set(self.baseline_optional_evidence)
        )
        if overlap:
            raise ValueError(
                "baseline evidence must not be both required and optional: " + ", ".join(overlap)
            )

    @property
    def capability_keys(self) -> tuple[str, ...]:
        return tuple(binding.capability_key for binding in self.available_evidence_capabilities)

    def capability(self, capability_key: str) -> EvidenceCapabilityBinding | None:
        for binding in self.available_evidence_capabilities:
            if binding.capability_key == capability_key:
                return binding
        return None

    def supports(self, evaluation_type: RuleEvaluationType) -> bool:
        return evaluation_type in self.supported_evaluation_types

    def to_dict(self) -> dict[str, object]:
        return {
            "control_key": self.control_key,
            "title": self.title,
            "description": self.description,
            "automation_support": self.automation_support.value,
            "supported_resource_types": list(self.supported_resource_types),
            "supported_evaluation_types": [
                value.value for value in self.supported_evaluation_types
            ],
            "available_evidence_capabilities": [
                binding.to_dict() for binding in self.available_evidence_capabilities
            ],
            "allowed_tool_bindings": list(self.allowed_tool_bindings),
            "baseline_required_evidence": list(self.baseline_required_evidence),
            "baseline_optional_evidence": list(self.baseline_optional_evidence),
            "severity_guidance": self.severity_guidance,
            "default_severity": self.default_severity.value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GovernanceControlCatalog:
    """The versioned boundary of what this product can evaluate.

    `version`은 Rule에 `control_catalog_version`으로 못 박힌다. Catalog가 개정되면 같은 문서라도
    다른 경계에서 추출된 것이므로, 재추출 시 fail-closed 판정의 identity에 들어간다.
    """

    version: str
    controls: tuple[GovernanceControl, ...]

    def __post_init__(self) -> None:
        require_non_empty_string(self.version, "version")
        if not self.controls:
            raise ValueError("controls must not be empty")
        seen: set[str] = set()
        for control in self.controls:
            if not isinstance(control, GovernanceControl):
                raise TypeError("controls items must be GovernanceControl values")
            if control.control_key in seen:
                raise ValueError(f"duplicate control {control.control_key!r}")
            seen.add(control.control_key)

    @property
    def control_keys(self) -> tuple[str, ...]:
        return tuple(control.control_key for control in self.controls)

    def control(self, control_key: str) -> GovernanceControl | None:
        for control in self.controls:
            if control.control_key == control_key:
                return control
        return None

    def automatable_controls(self) -> tuple[GovernanceControl, ...]:
        return tuple(
            control
            for control in self.controls
            if control.automation_support is ControlAutomationSupport.AVAILABLE
        )

    def manual_controls(self) -> tuple[GovernanceControl, ...]:
        return tuple(
            control
            for control in self.controls
            if control.automation_support is ControlAutomationSupport.MANUAL
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "controls": [control.to_dict() for control in self.controls],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedRequirement:
    """One Requirement the extractor claims a policy source states.

    **LLM은 source identity를 만들지 않는다.** `source_locators`만 돌려주고, 어느 Source의 어느
    version인지와 각 unit의 digest는 서버가 정규화 문서에서 조회해 붙인다. 모델이 digest를
    지어내면 Evidence는 검증 가능한 값이 아니라 모델의 주장이 된다.

    평가 결과 필드(`judgment`/`severity`/`score`/`source_score`/`anchor`)는 정의하지 않는다
    (`FORBIDDEN_EXTRACTION_FIELDS`). severity는 Catalog의 `default_severity`가 정하고 모델은
    `severity_guidance` 텍스트만 쓴다.
    """

    source_locators: tuple[str, ...]
    requirement: str
    requirement_summary: str
    classification: CandidateClassification
    mapping_reason: str
    mapped_control_key: str | None = None
    resource_types: tuple[str, ...] = ()
    evaluation_type: RuleEvaluationType | None = None
    applicability_semantics: str | None = None
    required_evidence: tuple[str, ...] = ()
    optional_evidence: tuple[str, ...] = ()
    evaluation_rubric: str | None = None
    severity_guidance: str | None = None
    exception_semantics: str | None = None
    compensating_control_semantics: str | None = None

    def __post_init__(self) -> None:
        _require_unique_strings(
            self.source_locators,
            "source_locators",
            limit=MAX_LOCATORS_PER_REQUIREMENT,
            allow_empty=False,
        )
        _require_length(self.requirement, "requirement", MAX_REQUIREMENT_LENGTH)
        _require_length(
            self.requirement_summary, "requirement_summary", MAX_REQUIREMENT_SUMMARY_LENGTH
        )
        _require_length(self.mapping_reason, "mapping_reason", MAX_MAPPING_REASON_LENGTH)
        for name, limit in (
            ("applicability_semantics", MAX_APPLICABILITY_SEMANTICS_LENGTH),
            ("evaluation_rubric", MAX_EVALUATION_RUBRIC_LENGTH),
            ("severity_guidance", MAX_SEVERITY_GUIDANCE_LENGTH),
            ("exception_semantics", MAX_EXCEPTION_SEMANTICS_LENGTH),
            ("compensating_control_semantics", MAX_COMPENSATING_CONTROL_SEMANTICS_LENGTH),
        ):
            _require_length(getattr(self, name), name, limit)
        if not isinstance(self.classification, CandidateClassification):
            raise TypeError("classification must be a CandidateClassification")
        require_optional_non_empty_string(self.mapped_control_key, "mapped_control_key")
        if self.evaluation_type is not None and not isinstance(
            self.evaluation_type, RuleEvaluationType
        ):
            raise TypeError("evaluation_type must be a RuleEvaluationType")
        for name in ("resource_types", "required_evidence", "optional_evidence"):
            _require_unique_strings(getattr(self, name), name, limit=MAX_EVIDENCE_CAPABILITIES)
        self._require_classification_shape()

    def _require_classification_shape(self) -> None:
        if self.classification is CandidateClassification.AUTOMATABLE:
            if self.mapped_control_key is None:
                raise ValueError("an AUTOMATABLE requirement must map to a control")
            if self.evaluation_type not in {
                RuleEvaluationType.IAC,
                RuleEvaluationType.AWS,
                RuleEvaluationType.HYBRID,
            }:
                raise ValueError(
                    "an AUTOMATABLE requirement must declare an IAC, AWS, or HYBRID evaluation type"
                )
            if not self.resource_types:
                raise ValueError("an AUTOMATABLE requirement must declare resource types")
            if not self.required_evidence:
                raise ValueError("an AUTOMATABLE requirement must declare required evidence")
            if self.evaluation_rubric is None:
                raise ValueError("an AUTOMATABLE requirement must declare an evaluation rubric")
            overlap = sorted(set(self.required_evidence) & set(self.optional_evidence))
            if overlap:
                raise ValueError(
                    "evidence must not be both required and optional: " + ", ".join(overlap)
                )
            return

        if self.classification is CandidateClassification.MANUAL:
            if self.mapped_control_key is None:
                raise ValueError("a MANUAL requirement must map to a MANUAL control")
            if self.evaluation_type is not RuleEvaluationType.MANUAL:
                raise ValueError("a MANUAL requirement must declare the MANUAL evaluation type")
            if self.required_evidence or self.optional_evidence:
                raise ValueError("a MANUAL requirement must not declare evidence")
            if self.resource_types:
                raise ValueError(
                    "a MANUAL requirement must not declare resource types — the server binds "
                    "it to the stable governance assessment resource"
                )
            if self.evaluation_rubric is not None:
                raise ValueError("a MANUAL requirement must not declare an evaluation rubric")
            return

        # UNSUPPORTED: 제품이 만들 수 있는 Rule이 없다. 어떤 실행 의미도 붙지 않는다.
        populated = [
            name
            for name in (
                "mapped_control_key",
                "resource_types",
                "evaluation_type",
                "required_evidence",
                "optional_evidence",
                "evaluation_rubric",
            )
            if getattr(self, name)
        ]
        if populated:
            raise ValueError(
                "an UNSUPPORTED requirement must not carry rule semantics: "
                + ", ".join(sorted(populated))
            )

    @property
    def digest(self) -> str:
        """A deterministic identity for this Requirement, stable across worker retries.

        같은 추출을 다시 저장할 때 같은 child item key를 쓰기 위한 값이다. 정렬된 locator와
        정규화된 requirement 텍스트만 사용해, 모델이 필드 순서를 바꿔도 같은 값이 나오게 한다.
        """
        canonical = "\x1f".join(
            (
                self.classification.value,
                self.mapped_control_key or "",
                *sorted(self.source_locators),
                " ".join(self.requirement.split()),
            )
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "source_locators": list(self.source_locators),
            "requirement": self.requirement,
            "requirement_summary": self.requirement_summary,
            "classification": self.classification.value,
            "mapping_reason": self.mapping_reason,
            "mapped_control_key": self.mapped_control_key,
            "resource_types": list(self.resource_types),
            "evaluation_type": (
                None if self.evaluation_type is None else self.evaluation_type.value
            ),
            "applicability_semantics": self.applicability_semantics,
            "required_evidence": list(self.required_evidence),
            "optional_evidence": list(self.optional_evidence),
            "evaluation_rubric": self.evaluation_rubric,
            "severity_guidance": self.severity_guidance,
            "exception_semantics": self.exception_semantics,
            "compensating_control_semantics": self.compensating_control_semantics,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptedRequirement:
    """A Requirement that survived validation, kept next to the Rule it became.

    Rule로 변환한 뒤에도 원 Requirement·분류·매핑 이유를 잃지 않는다. 리뷰어는 "이 Rule이 왜
    이렇게 생겼는가"를 Rule 필드만으로 재구성할 수 없다.
    """

    requirement: ExtractedRequirement
    candidate: RuleCandidate

    def __post_init__(self) -> None:
        if not isinstance(self.requirement, ExtractedRequirement):
            raise TypeError("requirement must be an ExtractedRequirement")
        if not isinstance(self.candidate, RuleCandidate):
            raise TypeError("candidate must be a RuleCandidate")
        if self.requirement.classification is CandidateClassification.UNSUPPORTED:
            raise ValueError("an UNSUPPORTED requirement must not carry a rule candidate")
        if self.candidate.rule.evaluation_type != self.requirement.evaluation_type:
            raise ValueError("the rule evaluation type must match the requirement")
        if self.candidate.rule.control_key != self.requirement.mapped_control_key:
            raise ValueError("the rule control key must match the requirement")

    @property
    def proposed_severity(self) -> RuleSeverity:
        """The read-only severity the Catalog proposed. 리뷰어는 승인하거나 후보를 거절한다."""
        return self.candidate.rule.severity

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement": self.requirement.to_dict(),
            "candidate": self.candidate.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectedRequirement:
    """A Requirement the validator refused, kept with its enumerated reasons.

    거절된 후보를 조용히 버리지 않고 보존한다. AUTOMATABLE 후보가 검증에 실패했다고 해서 자동으로
    MANUAL로 바꾸지 않는다 — 그것은 사람이 승인 가능한 Rule을 검증 실패로부터 만들어내는 일이다.
    """

    requirement: ExtractedRequirement
    rejection_codes: tuple[CandidateRejectionCode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.requirement, ExtractedRequirement):
            raise TypeError("requirement must be an ExtractedRequirement")
        if not self.rejection_codes:
            raise ValueError("rejection_codes must not be empty")
        seen: set[CandidateRejectionCode] = set()
        for code in self.rejection_codes:
            if not isinstance(code, CandidateRejectionCode):
                raise TypeError("rejection_codes items must be CandidateRejectionCode values")
            if code in seen:
                raise ValueError(f"duplicate rejection code {code.value}")
            seen.add(code)

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement": self.requirement.to_dict(),
            "rejection_codes": [code.value for code in self.rejection_codes],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthoringProvenance:
    """Everything that decided what this extraction produced.

    같은 source version을 다른 extractor/prompt/catalog로 재추출하면 결과가 달라진다. 그 사실을
    식별자로 남겨야, 저장 시점에 "같은 추출의 재시도"와 "다른 추출"을 구별해 fail-closed할 수 있다.
    """

    extractor_id: str
    extractor_version: str
    model_id: str
    model_version: str
    prompt_version: str
    candidate_schema_version: str
    control_catalog_version: str
    authoring_run_id: str
    requested_at: str

    _IDENTITY_FIELDS = (
        "extractor_id",
        "extractor_version",
        "prompt_version",
        "candidate_schema_version",
        "control_catalog_version",
    )

    def __post_init__(self) -> None:
        for name in (
            "extractor_id",
            "extractor_version",
            "model_id",
            "model_version",
            "prompt_version",
            "candidate_schema_version",
            "control_catalog_version",
            "authoring_run_id",
        ):
            require_non_empty_string(getattr(self, name), name)
        require_offset_aware_timestamp(self.requested_at, "requested_at")

    @property
    def extraction_identity(self) -> tuple[str, ...]:
        """The fields a re-extraction must match to be treated as the same extraction.

        `authoring_run_id`와 `model_version`은 여기 없다. 같은 추출의 worker 재시도는 run id를
        재사용하지만, 그 값이 identity에 들어가면 retry가 다른 추출로 보인다.
        """
        return tuple(getattr(self, name) for name in self._IDENTITY_FIELDS)

    def to_dict(self) -> dict[str, object]:
        return {
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "candidate_schema_version": self.candidate_schema_version,
            "control_catalog_version": self.control_catalog_version,
            "authoring_run_id": self.authoring_run_id,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyAuthoringResult:
    """The complete outcome of one authoring run over one exact source version.

    승인 가능한 Rule이 되는 것은 `accepted`와 `manual`뿐이다. `unsupported`와 `rejected`는
    보존되지만 Rule로 변환되지 않는다 — 보존과 승인 가능성은 다른 문제다.
    """

    document: NormalizedPolicyDocument
    accepted: tuple[AcceptedRequirement, ...] = ()
    manual: tuple[AcceptedRequirement, ...] = ()
    unsupported: tuple[ExtractedRequirement, ...] = ()
    rejected: tuple[RejectedRequirement, ...] = ()
    provenance: AuthoringProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.document, NormalizedPolicyDocument):
            raise TypeError("document must be a NormalizedPolicyDocument")
        if not self.document.is_approvable:
            raise ValueError("an authoring result requires a READY document")
        if not isinstance(self.provenance, AuthoringProvenance):
            raise TypeError("provenance must be an AuthoringProvenance")
        for entry in self.accepted:
            if not isinstance(entry, AcceptedRequirement):
                raise TypeError("accepted items must be AcceptedRequirement values")
            if entry.requirement.classification is not CandidateClassification.AUTOMATABLE:
                raise ValueError("accepted must contain only AUTOMATABLE requirements")
        for entry in self.manual:
            if not isinstance(entry, AcceptedRequirement):
                raise TypeError("manual items must be AcceptedRequirement values")
            if entry.requirement.classification is not CandidateClassification.MANUAL:
                raise ValueError("manual must contain only MANUAL requirements")
        for requirement in self.unsupported:
            if not isinstance(requirement, ExtractedRequirement):
                raise TypeError("unsupported items must be ExtractedRequirement values")
            if requirement.classification is not CandidateClassification.UNSUPPORTED:
                raise ValueError("unsupported must contain only UNSUPPORTED requirements")
        for entry in self.rejected:
            if not isinstance(entry, RejectedRequirement):
                raise TypeError("rejected items must be RejectedRequirement values")
        self._require_unique_rule_versions()

    def _require_unique_rule_versions(self) -> None:
        seen: set[tuple[str, str]] = set()
        for entry in self.approvable:
            key = (entry.candidate.rule.rule_id, entry.candidate.rule.version)
            if key in seen:
                raise ValueError("an authoring result must not duplicate a rule version")
            seen.add(key)

    @property
    def approvable(self) -> tuple[AcceptedRequirement, ...]:
        """The requirements that become approvable Rules: AUTOMATABLE and MANUAL only."""
        return self.accepted + self.manual

    @property
    def candidates(self) -> tuple[RuleCandidate, ...]:
        return tuple(entry.candidate for entry in self.approvable)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "accepted": len(self.accepted),
            "manual": len(self.manual),
            "unsupported": len(self.unsupported),
            "rejected": len(self.rejected),
        }

    @property
    def result_digest(self) -> str:
        """A digest over what this run produced, used to verify a multi-item write.

        후보를 여러 DynamoDB item에 나눠 쓰면, 일부만 써지고 manifest가 READY가 되는 상태가
        생길 수 있다. 개수만 세면 "다른 후보가 같은 개수만큼 써진" 경우를 통과시키므로
        내용까지 포함한 digest를 manifest에 남기고 전환 전에 대조한다.
        """
        parts: list[str] = [f"{name}={count}" for name, count in sorted(self.counts.items())]
        parts.extend(sorted(f"accepted:{entry.requirement.digest}" for entry in self.accepted))
        parts.extend(sorted(f"manual:{entry.requirement.digest}" for entry in self.manual))
        parts.extend(
            sorted(f"unsupported:{requirement.digest}" for requirement in self.unsupported)
        )
        parts.extend(sorted(f"rejected:{entry.requirement.digest}" for entry in self.rejected))
        return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "document": self.document.to_dict(),
            "accepted": [entry.to_dict() for entry in self.accepted],
            "manual": [entry.to_dict() for entry in self.manual],
            "unsupported": [requirement.to_dict() for requirement in self.unsupported],
            "rejected": [entry.to_dict() for entry in self.rejected],
            "provenance": self.provenance.to_dict(),
        }


class AuthoringRunStatus(StrEnum):
    """Processing state of one authoring run over one exact source version.

    Review와 Approval은 `READY`만 읽는다. 후보가 여러 item에 나뉘어 저장되므로, 중간 상태를
    읽으면 일부만 쓰인 후보 집합을 완전한 것으로 착각한다.
    """

    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthoringManifest:
    """The single item that says whether an authoring run's candidates are complete.

    `normalized_sha256`과 `provenance.extraction_identity`가 함께 이 실행의 identity를 이룬다.
    같은 source version을 다른 extractor·prompt·Catalog로 재추출하면 identity가 달라지고,
    저장 계층은 그것을 재시도가 아니라 **다른 추출**로 보아 fail-closed한다.
    """

    source_id: str
    source_version: str
    normalized_sha256: str
    status: AuthoringRunStatus
    provenance: AuthoringProvenance
    counts: dict[str, int] = field(default_factory=dict)
    result_digest: str | None = None
    failure_code: ArtifactReadFailureCode | None = None

    _COUNT_KEYS = ("accepted", "manual", "unsupported", "rejected")

    def __post_init__(self) -> None:
        for name in ("source_id", "source_version", "normalized_sha256"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.status, AuthoringRunStatus):
            raise TypeError("status must be an AuthoringRunStatus")
        if not isinstance(self.provenance, AuthoringProvenance):
            raise TypeError("provenance must be an AuthoringProvenance")
        require_optional_non_empty_string(self.result_digest, "result_digest")
        if self.failure_code is not None and not isinstance(
            self.failure_code, ArtifactReadFailureCode
        ):
            raise TypeError("failure_code must be an ArtifactReadFailureCode")
        for key, value in self.counts.items():
            if key not in self._COUNT_KEYS:
                raise ValueError(f"unknown count {key!r}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"count {key!r} must be a non-negative integer")

        if self.status is AuthoringRunStatus.READY:
            if self.result_digest is None:
                raise ValueError("a READY manifest must carry a result_digest")
            if sorted(self.counts) != sorted(self._COUNT_KEYS):
                raise ValueError("a READY manifest must carry every count")
            if self.failure_code is not None:
                raise ValueError("a READY manifest must not carry a failure_code")
        elif self.status is AuthoringRunStatus.FAILED:
            if self.failure_code is None:
                raise ValueError("a FAILED manifest must carry a failure_code")
        elif self.failure_code is not None:
            raise ValueError("failure_code is only valid on a FAILED manifest")

    @property
    def is_reviewable(self) -> bool:
        """Whether review and approval may read this run's candidates."""
        return self.status is AuthoringRunStatus.READY

    @property
    def extraction_identity(self) -> tuple[str, ...]:
        """What a re-extraction of this source version must match to count as a retry."""
        return (self.normalized_sha256, *self.provenance.extraction_identity)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "normalized_sha256": self.normalized_sha256,
            "status": self.status.value,
            "counts": dict(self.counts),
            "result_digest": self.result_digest,
            "failure_code": None if self.failure_code is None else self.failure_code.value,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyAuthoringRequest:
    """The queue payload that asks a worker to extract candidates from one source version.

    정책 텍스트도 S3 key도 담지 않는다. worker는 `customer_id`와 source 판본만 받아, 보호된
    정규화 artifact를 자기 권한으로 다시 읽는다. payload에 텍스트를 담으면 queue와 DLQ와
    queue 로그가 전부 정책 원문의 사본이 된다.

    `requested_at`은 **최초 요청 시각에 고정**한다. worker 재시도가 새 시각을 만들면 같은 실행이
    다른 provenance를 갖게 되고, 저장 계층은 그것을 재시도로 알아보지 못한다.
    """

    customer_id: str
    source_id: str
    source_version: str
    authoring_run_id: str
    requested_at: str

    def __post_init__(self) -> None:
        for name in ("customer_id", "source_id", "source_version", "authoring_run_id"):
            require_non_empty_string(getattr(self, name), name)
        require_offset_aware_timestamp(self.requested_at, "requested_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "customer_id": self.customer_id,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "authoring_run_id": self.authoring_run_id,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateReviewEntry:
    """What a reviewer sees for one approvable candidate.

    `proposed_severity`는 **read-only**다. Catalog가 정한 값이며 리뷰어는 그것을 승인하거나
    후보를 거절한다 — 화면에서 등급을 고르게 하면 AI가 만든 근거와 사람이 정한 등급이 섞여,
    나중에 누가 무엇을 정했는지 말할 수 없다.

    `locators`는 서버가 정규화 문서에서 만든 `SourceReference`다. 모델이 준 것은 locator뿐이고
    digest는 여기 붙어서야 처음 등장한다.
    """

    rule_id: str
    rule_version: str
    classification: CandidateClassification
    requirement: str
    requirement_summary: str
    mapping_reason: str
    control_key: str
    evaluation_type: RuleEvaluationType
    proposed_severity: RuleSeverity
    locators: tuple[SourceReference, ...]
    resource_types: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    optional_evidence: tuple[str, ...] = ()
    applicability_semantics: str | None = None
    evaluation_rubric: str | None = None
    severity_guidance: str | None = None
    exception_semantics: str | None = None
    compensating_control_semantics: str | None = None

    @classmethod
    def from_accepted(cls, accepted: AcceptedRequirement) -> "CandidateReviewEntry":
        if not isinstance(accepted, AcceptedRequirement):
            raise TypeError("accepted must be an AcceptedRequirement")
        rule = accepted.candidate.rule
        requirement = accepted.requirement
        if rule.control_key is None or rule.evaluation_type is None:
            raise ValueError("an approvable candidate must carry execution semantics")
        return cls(
            rule_id=rule.rule_id,
            rule_version=rule.version,
            classification=requirement.classification,
            requirement=requirement.requirement,
            requirement_summary=requirement.requirement_summary,
            mapping_reason=requirement.mapping_reason,
            control_key=rule.control_key,
            evaluation_type=rule.evaluation_type,
            proposed_severity=rule.severity,
            locators=rule.source_references,
            resource_types=rule.resource_types,
            required_evidence=rule.required_evidence,
            optional_evidence=rule.optional_evidence,
            applicability_semantics=rule.applicability_semantics,
            evaluation_rubric=rule.evaluation_rubric,
            severity_guidance=rule.severity_guidance,
            exception_semantics=rule.exception_semantics,
            compensating_control_semantics=rule.compensating_control_semantics,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "classification": self.classification.value,
            "requirement": self.requirement,
            "requirement_summary": self.requirement_summary,
            "mapping_reason": self.mapping_reason,
            "control_key": self.control_key,
            "evaluation_type": self.evaluation_type.value,
            "proposed_severity": self.proposed_severity.value,
            "locators": [reference.to_dict() for reference in self.locators],
            "resource_types": list(self.resource_types),
            "required_evidence": list(self.required_evidence),
            "optional_evidence": list(self.optional_evidence),
            "applicability_semantics": self.applicability_semantics,
            "evaluation_rubric": self.evaluation_rubric,
            "severity_guidance": self.severity_guidance,
            "exception_semantics": self.exception_semantics,
            "compensating_control_semantics": self.compensating_control_semantics,
        }
