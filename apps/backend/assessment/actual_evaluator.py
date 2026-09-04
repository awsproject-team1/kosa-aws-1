"""M1 Actual evaluator composed from read-only evidence and Bedrock output guards.

**모델을 부르기 전에 근거 유무를 판정한다 (ADR-0023 §2).** AWS Actual adapter는 필드 allow-list로
투영된 구조화 문서를 돌려주므로, 승인된 Rule의 `required_evidence`가 가리키는
`document_paths`가 실제로 채워졌는지를 결정적으로 확인할 수 있다. 확인 없이 모델을 부르면
"근거가 없어서 판단 못 함"과 "모델이 판정을 회피함"이 같은 결과로 섞인다. 근거가 빠진 좌표는
Code가 `INSUFFICIENT_EVIDENCE`를 만들고 Bedrock을 호출하지 않는다.

**게이트는 fail-closed다 (2026-09-05).** 처음에는 Rule이 요구한 capability에 이 resource type의
AWS_ACTUAL binding이 없으면 검사를 건너뛰고 모델에게 물었다. 그러면 Catalog가 "이 Control은
AWS 근거가 없다"고 이미 아는 좌표에서 모델이 문서 전체를 뒤져 다른 field를 근거로 인용한다 —
baseline Profile의 S3 ACL Rule이 public-access-block 플래그를 근거로 PASS를 낸 것이 그 경우다.
선언이 없는 것은 "해석이 필요하다"가 아니라 "근거가 없다"이므로, 그 좌표는 모델 호출 없이
`INSUFFICIENT_EVIDENCE`다.

legacy Rule(`evaluation_type is None`)도 같은 게이트를 지난다. Catalog는 legacy Rule이 어떤
Control을 구현하는지 알고 있다(`control_for_rule`); 그것을 모른 척하면 배포된 baseline Profile
전체가 게이트 밖에 남는다. Catalog가 모르는 legacy Rule만 이전처럼 모델로 간다. IaC hint는
authoritative가 아니므로 이 gate의 대상이 아니다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.backend.assessment.actual import ActualEvidence, ActualEvidenceLoader
from apps.backend.assessment.bedrock import BedrockConverseClient, BedrockStructuredEvaluator
from apps.backend.assessment.deterministic import (
    aws_bindings,
    decidable_bindings_for,
    decide,
    result_from_verdict,
)
from apps.backend.policy import PolicyContext
from apps.backend.policy.control_catalog import (
    MVP_CONTROL_CATALOG,
    RuleControlLookupError,
    control_for_rule,
)
from apps.backend.policy.evidence_paths import missing_document_paths
from packages.contracts import (
    DecisionSource,
    EvaluationResult,
    EvaluationStatus,
    GovernanceControl,
    GovernanceControlCatalog,
    ModelProfile,
    PolicyRule,
    ScoringMode,
)

#: 근거가 없어 판정하지 못한 좌표의 점수. readiness 평균에는 들어가지만, 그 값이 "위반"이 아니라
#: "확인 불가"라는 사실은 status가 말한다.
INSUFFICIENT_EVIDENCE_SCORE = 0.0


class ActualEvidenceGateError(ValueError):
    """Raised when an approved Rule's evidence capability cannot be checked at all."""


class ActualBedrockEvaluator:
    """An AssessmentRunner-compatible evaluator for one Actual Resource × Rule.

    The resource type is fixed by the injected loader, so a runner that was given an EC2
    target cannot end up evaluating an S3 document.
    """

    def __init__(
        self,
        *,
        evidence_loader: ActualEvidenceLoader,
        client: BedrockConverseClient,
        catalog: GovernanceControlCatalog = MVP_CONTROL_CATALOG,
    ) -> None:
        if not isinstance(evidence_loader, ActualEvidenceLoader):
            raise TypeError("evidence_loader must be an ActualEvidenceLoader")
        if client is None:
            raise TypeError("client is required")
        if not isinstance(catalog, GovernanceControlCatalog):
            raise TypeError("catalog must be a GovernanceControlCatalog")
        self._evidence_loader = evidence_loader
        self._client = client
        self._catalog = catalog

    @property
    def resource_type(self) -> str:
        return self._evidence_loader.resource_type

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        if not isinstance(rule, PolicyRule):
            raise TypeError("rule must be a PolicyRule")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        evidence = self._evidence_loader.load(resource_id)
        gap = self.evidence_gap(rule, evidence.resource_document)
        if gap is not None and gap.blocks_judgment:
            return _insufficient_evidence(
                resource_id=resource_id,
                rule=rule,
                evidence=evidence,
                model_profile=model_profile,
                reason=gap.reason,
            )
        # 선언된 술어만으로 답할 수 있으면 모델을 부르지 않는다. 사실을 모델에게 물으면
        # 정확도·점수 입도·비용을 한꺼번에 잃는다(`deterministic` 모듈 참조). 술어가 없는
        # capability가 하나라도 있으면 통째로 아래 모델 경로로 간다.
        if gap is not None:
            bindings = decidable_bindings_for(
                gap.control, gap.required, resource_type=self.resource_type
            )
            if bindings:
                return result_from_verdict(
                    decide(bindings, evidence.resource_document),
                    resource_id=resource_id,
                    rule=rule,
                    evidence_references=evidence.evidence_references,
                    model_profile=model_profile,
                )
        return BedrockStructuredEvaluator(
            client=self._client,
            perspective=evidence.perspective,
            resource_document=evidence.resource_document,
            evidence_references=evidence.evidence_references,
        ).evaluate(
            resource_id=resource_id,
            rule=rule,
            context=context,
            model_profile=model_profile,
        )

    def evidence_gap(
        self, rule: PolicyRule, resource_document: Mapping[str, object]
    ) -> EvidenceGap | None:
        """What this read lacks against the Rule's required capabilities, or `None` if the
        catalog has no knowledge of the Rule at all.

        요구 capability 각각에 대해 두 가지를 본다: 이 resource type의 AWS_ACTUAL binding이
        **선언돼 있는가**, 선언돼 있다면 그 `document_paths`가 **채워졌는가**. 전자가 없으면
        Catalog 스스로 "이 관점의 근거가 없다"고 말하는 것이고, 후자가 비면 이번 read가 그 값을
        가져오지 못한 것이다. 둘 다 모델에게 물을 일이 아니다.

        IaC hint는 authoritative가 아니고 다른 resource type의 capability는 이 문서에 나타날 수
        없으므로, 두 경우 모두 AWS_ACTUAL·이 resource type의 binding만 본다. 단, 그 capability가
        Catalog 어딘가에 IAC binding으로 존재하면 그것은 "다른 관점의 근거"이지 "AWS 근거 미선언"과
        같은 것이다 — 이 관점에서 답할 수 없다는 사실은 같다.
        """
        try:
            resolved = control_for_rule(rule, self._catalog)
        except RuleControlLookupError as error:
            raise ActualEvidenceGateError(str(error)) from error
        if resolved is None:
            return None
        control, required = resolved
        bindings = aws_bindings(control, resource_type=self.resource_type)
        undeclared: list[str] = []
        missing: list[str] = []
        for capability_key in required:
            binding = bindings.get(capability_key)
            if binding is None:
                undeclared.append(capability_key)
                continue
            for path in missing_document_paths(resource_document, binding.document_paths):
                if path not in missing:
                    missing.append(path)
        return EvidenceGap(
            control=control,
            required=required,
            undeclared_capabilities=tuple(undeclared),
            missing_paths=tuple(missing),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceGap:
    """The pre-flight verdict on one Actual read against one Rule's evidence requirements."""

    control: GovernanceControl
    required: tuple[str, ...]
    undeclared_capabilities: tuple[str, ...]
    missing_paths: tuple[str, ...]

    @property
    def blocks_judgment(self) -> bool:
        return bool(self.undeclared_capabilities or self.missing_paths)

    @property
    def reason(self) -> str:
        """Why no judgment was made — schema names only, never customer values."""
        parts: list[str] = []
        if self.undeclared_capabilities:
            parts.append(
                "the catalog declares no AWS_ACTUAL evidence for "
                + ", ".join(self.undeclared_capabilities)
            )
        if self.missing_paths:
            parts.append("the read did not carry " + ", ".join(self.missing_paths))
        return "; ".join(parts)


def _insufficient_evidence(
    *,
    resource_id: str,
    rule: PolicyRule,
    evidence: ActualEvidence,
    model_profile: ModelProfile,
    reason: str,
) -> EvaluationResult:
    """Record that the read happened and what it lacked, without calling the model.

    rationale에는 capability key와 문서 경로 이름만 들어간다 — 둘 다 Catalog와 adapter
    projection의 schema이지 고객 데이터가 아니다. evidence에는 실제로 수행한 read의 locator와
    Rule이 인용한 정책 판본을 남겨, "무엇을 읽었고 무엇이 비어 있었는가"가 결과에서 복원된다.
    """
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule.rule_id,
        perspective=evidence.perspective,
        status=EvaluationStatus.INSUFFICIENT_EVIDENCE,
        severity=rule.severity.value,
        score=INSUFFICIENT_EVIDENCE_SCORE,
        rationale=(
            "The read-only Actual read cannot support a judgment on this rule: "
            f"{reason}. The model was not asked to judge it."
        ),
        evidence_references=(
            *evidence.evidence_references,
            *(reference.evidence_reference for reference in rule.source_references),
        ),
        rule_version=rule.version,
        rubric_version=model_profile.rubric_version,
        model_profile_id=model_profile.model_profile_id,
        scoring_mode=ScoringMode.CONTINUOUS,
        decided_by=DecisionSource.CODE,
    )
