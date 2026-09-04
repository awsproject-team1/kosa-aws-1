"""M1 Actual evaluator composed from read-only evidence and Bedrock output guards.

**모델을 부르기 전에 근거 유무를 판정한다 (ADR-0023 §2).** AWS Actual adapter는 필드 allow-list로
투영된 구조화 문서를 돌려주므로, 승인된 Rule의 `required_evidence`가 가리키는
`document_paths`가 실제로 채워졌는지를 결정적으로 확인할 수 있다. 확인 없이 모델을 부르면
"근거가 없어서 판단 못 함"과 "모델이 판정을 회피함"이 같은 결과로 섞인다. 근거가 빠진 좌표는
Code가 `INSUFFICIENT_EVIDENCE`를 만들고 Bedrock을 호출하지 않는다.

legacy Rule(`evaluation_type is None`)은 evidence capability를 갖지 않으므로 gate 없이 이전과
같이 평가된다. IaC hint는 authoritative가 아니므로 이 gate의 대상이 아니다.
"""

from __future__ import annotations

from collections.abc import Mapping

from apps.backend.assessment.actual import ActualEvidence, ActualEvidenceLoader
from apps.backend.assessment.bedrock import BedrockConverseClient, BedrockStructuredEvaluator
from apps.backend.assessment.deterministic import (
    decidable_bindings,
    decide,
    result_from_verdict,
)
from apps.backend.policy import PolicyContext
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG
from apps.backend.policy.evidence_paths import missing_document_paths
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
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
        missing = self.missing_required_evidence(rule, evidence.resource_document)
        if missing:
            return _insufficient_evidence(
                resource_id=resource_id,
                rule=rule,
                evidence=evidence,
                model_profile=model_profile,
                missing=missing,
            )
        # 선언된 술어만으로 답할 수 있으면 모델을 부르지 않는다. 사실을 모델에게 물으면
        # 정확도·점수 입도·비용을 한꺼번에 잃는다(`deterministic` 모듈 참조). 술어가 없는
        # capability가 하나라도 있으면 통째로 아래 모델 경로로 간다.
        bindings = decidable_bindings(self._catalog, rule, resource_type=self.resource_type)
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

    def missing_required_evidence(
        self, rule: PolicyRule, resource_document: Mapping[str, object]
    ) -> tuple[str, ...]:
        """Return the declared AWS document paths this read did not carry, in order.

        Rule의 `required_evidence`는 Catalog capability key다. 그중 이 resource type의
        AWS_ACTUAL binding만 pre-flight 대상이다 — IaC hint는 authoritative가 아니고, 다른
        resource type의 capability는 이 문서에 나타날 수 없다. 승인된 Rule이 Catalog에 없는
        Control을 가리키면 검사 자체가 불가능하므로 fail-closed한다.
        """
        if rule.evaluation_type is None or not rule.required_evidence:
            return ()
        control = self._catalog.control(rule.control_key or "")
        if control is None:
            raise ActualEvidenceGateError(
                f"approved rule {rule.rule_id!r} names a control the catalog does not declare"
            )
        bindings = {
            binding.capability_key: binding
            for binding in control.available_evidence_capabilities
            if binding.perspective is EvaluationPerspective.AWS_ACTUAL
            and binding.resource_type == self.resource_type
        }
        missing: list[str] = []
        for capability_key in rule.required_evidence:
            binding = bindings.get(capability_key)
            if binding is None:
                continue
            for path in missing_document_paths(resource_document, binding.document_paths):
                if path not in missing:
                    missing.append(path)
        return tuple(missing)


def _insufficient_evidence(
    *,
    resource_id: str,
    rule: PolicyRule,
    evidence: ActualEvidence,
    model_profile: ModelProfile,
    missing: tuple[str, ...],
) -> EvaluationResult:
    """Record that the read happened and what it lacked, without calling the model.

    rationale에는 문서 경로 이름만 들어간다 — 경로는 adapter projection의 schema이지 고객
    데이터가 아니다. evidence에는 실제로 수행한 read의 locator와 Rule이 인용한 정책 판본을
    남겨, "무엇을 읽었고 무엇이 비어 있었는가"가 결과에서 복원된다.
    """
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule.rule_id,
        perspective=evidence.perspective,
        status=EvaluationStatus.INSUFFICIENT_EVIDENCE,
        severity=rule.severity.value,
        score=INSUFFICIENT_EVIDENCE_SCORE,
        rationale=(
            "The read-only Actual read did not carry the evidence this rule requires "
            f"({', '.join(missing)}); the model was not asked to judge it."
        ),
        evidence_references=(
            *evidence.evidence_references,
            *(reference.evidence_reference for reference in rule.source_references),
        ),
        rule_version=rule.version,
        rubric_version=model_profile.rubric_version,
        model_profile_id=model_profile.model_profile_id,
        scoring_mode=ScoringMode.CONTINUOUS,
    )
