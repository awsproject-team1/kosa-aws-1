"""Validate one extracted Requirement against the Catalog and build the Rule it becomes.

검증 순서는 고정돼 있다.

    Locator 검증
    → 분류 불변식
    → Control/resource/evaluation type 검증
    → Evidence capability 검증
    → Rule build

**Evidence를 조용히 제거하지 않는다.** AI가 Catalog 밖 capability를 요구하면 그 항목을 빼고
Rule을 만드는 것이 아니라 후보 자체를 거절한다. 빼고 만들면, 리뷰어가 승인한 Rule과 AI가
제안한 Rule이 다른 것이 되고 그 차이는 아무 데도 기록되지 않는다.

**AUTOMATABLE이 검증에 실패해도 MANUAL로 바꾸지 않는다.** 그것은 검증 실패로부터 승인 가능한
Rule을 만들어내는 일이다. 실패한 후보는 rejection code와 함께 보존한다.

**severity는 AI가 정하지 않는다.** Catalog의 `default_severity`가 정하고, AI는
`severity_guidance` 텍스트만 쓴다. `SourceReference`도 AI 출력에서 복사하지 않는다 — locator만
받아 서버가 정규화 문서에서 digest를 조회해 만든다.
"""

from __future__ import annotations

from hashlib import sha256

from apps.backend.policy.control_catalog import GOVERNANCE_ASSESSMENT_RESOURCE_TYPE
from apps.backend.policy.ingestion.pipeline import source_reference_for
from packages.contracts import (
    AcceptedRequirement,
    AssessmentPhase,
    CandidateClassification,
    CandidateRejectionCode,
    ControlAutomationSupport,
    ExtractedRequirement,
    GovernanceControl,
    GovernanceControlCatalog,
    NormalizedPolicyDocument,
    PolicyRule,
    RejectedRequirement,
    RuleCandidate,
    RuleEvaluationType,
)

#: 실행 유형별 적용 Phase. IAC와 MANUAL도 `POST_DEPLOY_VERIFICATION`에 들어간다 — Initial과
#: Verification의 planned set이 같아야 비교가 성립하기 때문이다. AWS는 배포 전 계획 단계에
#: 실제 리소스가 없으므로 `DEPLOYMENT_READINESS`를 갖지 않는다.
APPLICABLE_PHASES: dict[RuleEvaluationType, tuple[AssessmentPhase, ...]] = {
    RuleEvaluationType.IAC: (
        AssessmentPhase.INITIAL,
        AssessmentPhase.DEPLOYMENT_READINESS,
        AssessmentPhase.POST_DEPLOY_VERIFICATION,
    ),
    RuleEvaluationType.AWS: (
        AssessmentPhase.INITIAL,
        AssessmentPhase.POST_DEPLOY_VERIFICATION,
    ),
    RuleEvaluationType.HYBRID: (
        AssessmentPhase.INITIAL,
        AssessmentPhase.DEPLOYMENT_READINESS,
        AssessmentPhase.POST_DEPLOY_VERIFICATION,
    ),
    RuleEvaluationType.MANUAL: (
        AssessmentPhase.INITIAL,
        AssessmentPhase.POST_DEPLOY_VERIFICATION,
    ),
}

_RULE_ID_DIGEST_LENGTH = 12


def build_candidate(
    *,
    requirement: ExtractedRequirement,
    document: NormalizedPolicyDocument,
    catalog: GovernanceControlCatalog,
) -> AcceptedRequirement | RejectedRequirement:
    """Validate one Requirement and return either the Rule it becomes or why it was refused."""
    if not isinstance(requirement, ExtractedRequirement):
        raise TypeError("requirement must be an ExtractedRequirement")
    if requirement.classification is CandidateClassification.UNSUPPORTED:
        raise ValueError("an UNSUPPORTED requirement is not a rule candidate")

    codes = _validate(requirement=requirement, document=document, catalog=catalog)
    if codes:
        return RejectedRequirement(requirement=requirement, rejection_codes=tuple(codes))

    control = catalog.control(requirement.mapped_control_key or "")
    assert control is not None  # _validate가 통과했으면 Control은 존재한다.
    rule = _build_rule(
        requirement=requirement,
        document=document,
        control=control,
        catalog_version=catalog.version,
    )
    return AcceptedRequirement(requirement=requirement, candidate=RuleCandidate(rule=rule))


def _validate(
    *,
    requirement: ExtractedRequirement,
    document: NormalizedPolicyDocument,
    catalog: GovernanceControlCatalog,
) -> list[CandidateRejectionCode]:
    codes: list[CandidateRejectionCode] = []

    # 1. Locator — AI가 지어낸 locator는 문서에 없다.
    if any(document.unit(locator) is None for locator in requirement.source_locators):
        codes.append(CandidateRejectionCode.UNKNOWN_LOCATOR)

    # 2. Control 존재.
    control = catalog.control(requirement.mapped_control_key or "")
    if control is None:
        codes.append(CandidateRejectionCode.UNKNOWN_CONTROL_KEY)
        return codes

    # 3. 분류와 Control의 자동화 지원이 서로 모순되지 않아야 한다.
    codes.extend(_classification_conflicts(requirement, control))

    if requirement.classification is CandidateClassification.MANUAL:
        # MANUAL은 evidence를 갖지 않는다. Contract가 이미 막지만, Rule Builder도 자기 입력을
        # 스스로 확인한다 — 이 함수는 Contract를 우회해 만들어진 값도 받을 수 있다.
        if requirement.required_evidence or requirement.optional_evidence:
            codes.append(CandidateRejectionCode.MANUAL_RULE_WITH_EVIDENCE)
        return codes

    # 4. resource type과 evaluation type이 Control이 지원하는 범위 안이어야 한다.
    unsupported_types = set(requirement.resource_types) - set(control.supported_resource_types)
    if unsupported_types:
        codes.append(CandidateRejectionCode.UNSUPPORTED_RESOURCE_TYPE)
    if requirement.evaluation_type is None or not control.supports(requirement.evaluation_type):
        codes.append(CandidateRejectionCode.UNSUPPORTED_EVALUATION_TYPE)

    # 5. Evidence — Catalog 밖 capability는 제거하지 않고 거절한다.
    available = set(control.capability_keys)
    requested = set(requirement.required_evidence) | set(requirement.optional_evidence)
    if requested - available:
        codes.append(CandidateRejectionCode.EVIDENCE_CAPABILITY_NOT_AVAILABLE)
    if not (set(requirement.required_evidence) | set(control.baseline_required_evidence)):
        codes.append(CandidateRejectionCode.MISSING_REQUIRED_EVIDENCE)
    return codes


def _classification_conflicts(
    requirement: ExtractedRequirement, control: GovernanceControl
) -> list[CandidateRejectionCode]:
    """Refuse a mapping whose classification and control automation disagree.

    MANUAL Requirement가 자동화 가능한 Control을 가리키면 사람이 검토해야 할 것이 자동 평가로
    계획되고, AUTOMATABLE이 MANUAL/KNOWN_UNSUPPORTED Control을 가리키면 실행 경로가 없는 Rule이
    승인 가능해진다. 둘 다 조용히 고치지 않고 거절한다.
    """
    support = control.automation_support
    if requirement.classification is CandidateClassification.MANUAL:
        if support is not ControlAutomationSupport.MANUAL:
            return [CandidateRejectionCode.CLASSIFICATION_MAPPING_CONFLICT]
        return []
    if support is not ControlAutomationSupport.AVAILABLE:
        return [CandidateRejectionCode.CLASSIFICATION_MAPPING_CONFLICT]
    return []


def _build_rule(
    *,
    requirement: ExtractedRequirement,
    document: NormalizedPolicyDocument,
    control: GovernanceControl,
    catalog_version: str,
) -> PolicyRule:
    evaluation_type = requirement.evaluation_type
    assert evaluation_type is not None  # 검증을 통과한 후보는 실행 유형을 갖는다.

    # SourceReference는 AI 출력에서 복사하지 않는다. locator만 받아 서버가 digest를 조회한다.
    references = tuple(
        source_reference_for(document, locator) for locator in sorted(requirement.source_locators)
    )

    if evaluation_type is RuleEvaluationType.MANUAL:
        resource_types: tuple[str, ...] = (GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,)
        required_evidence: tuple[str, ...] = ()
        optional_evidence: tuple[str, ...] = ()
    else:
        resource_types = tuple(requirement.resource_types)
        required_evidence = _union(
            control.baseline_required_evidence, requirement.required_evidence
        )
        # baseline required와 겹치는 optional 요청은 required가 이긴다 — 한 capability가 양쪽에
        # 있으면 Rule Contract가 거부하고, 더 강한 요구를 낮추는 쪽이 위험하다.
        optional_evidence = tuple(
            key
            for key in _union(control.baseline_optional_evidence, requirement.optional_evidence)
            if key not in required_evidence
        )

    return PolicyRule(
        rule_id=_rule_id(requirement=requirement, document=document),
        version=document.source_version,
        title=requirement.requirement_summary,
        severity=control.default_severity,
        applicable_phases=APPLICABLE_PHASES[evaluation_type],
        resource_types=resource_types,
        source_references=references,
        control_key=control.control_key,
        control_catalog_version=catalog_version,
        evaluation_type=evaluation_type,
        applicability_semantics=requirement.applicability_semantics,
        required_evidence=required_evidence,
        optional_evidence=optional_evidence,
        evaluation_rubric=requirement.evaluation_rubric,
        severity_guidance=requirement.severity_guidance,
        exception_semantics=requirement.exception_semantics,
        compensating_control_semantics=requirement.compensating_control_semantics,
    )


def _union(baseline: tuple[str, ...], requested: tuple[str, ...]) -> tuple[str, ...]:
    """`baseline ∪ requested`, baseline 순서를 먼저 유지한 결정적 순서."""
    merged = list(baseline)
    for key in requested:
        if key not in merged:
            merged.append(key)
    return tuple(merged)


def _rule_id(*, requirement: ExtractedRequirement, document: NormalizedPolicyDocument) -> str:
    """A canonical, deterministic Rule ID for one Requirement in one source version.

    같은 문서를 같은 Catalog·prompt로 다시 추출하면 같은 Rule ID가 나와야 한다. worker 재시도가
    새 Rule을 만들면, 승인 화면에 같은 내용의 후보가 둘 생기고 둘 다 승인될 수 있다.
    """
    evaluation_type = requirement.evaluation_type
    assert evaluation_type is not None
    canonical = "\x1f".join(
        (
            document.source_id,
            requirement.mapped_control_key or "",
            evaluation_type.value,
            "\x1e".join(sorted(requirement.source_locators)),
            " ".join(requirement.requirement.split()),
        )
    )
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    return f"CUST-{requirement.mapped_control_key}-{digest[:_RULE_ID_DIGEST_LENGTH]}"
