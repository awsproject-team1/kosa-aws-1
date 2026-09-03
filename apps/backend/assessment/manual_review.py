"""Record a MANUAL Rule as an evaluation coordinate a person still has to settle.

**이 evaluator는 아무것도 호출하지 않는다.** Bedrock도, AWS Resource Tool도, GitHub Tool도.
사람이 검토해야 한다고 승인된 통제에 대해 모델을 부르면, 그 결과는 도구가 관찰한 사실이 아니라
모델의 추측이면서 다른 평가 결과와 똑같이 생겼다.

그러면 왜 결과를 만드는가. **좌표를 남기기 위해서다.** MANUAL 통제를 결과에서 빼면 Coverage가
그것을 아예 모르고, Initial과 Post-Deploy Verification의 planned set이 달라져 비교가 성립하지
않는다. 그래서 `MANUAL_REVIEW` 상태의 결과를 만들되 숫자 readiness 평균에서는 제외한다
(`_NON_SCORING_PERSPECTIVES`).

대상 좌표는 Repository 단위로 **안정적**이어야 한다. Assessment ID를 resource ID로 쓰면 같은
Repository의 Initial과 Verification이 서로 다른 좌표를 갖게 되어, 정확히 비교하려고 만든 결과가
비교를 불가능하게 만든다.
"""

from __future__ import annotations

from apps.backend.policy import PolicyContext
from apps.backend.policy.control_catalog import GOVERNANCE_ASSESSMENT_RESOURCE_TYPE
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    PolicyRule,
    RuleEvaluationType,
    ScoringMode,
)

#: 고정 rationale. 모델이 쓰지 않으므로 문장이 실행마다 달라질 이유가 없고, 달라지면 같은 상태를
#: 서로 다른 결과처럼 보이게 한다.
MANUAL_REVIEW_RATIONALE = (
    "This requirement is settled by human review: no tool in this product observes it. "
    "The coordinate is recorded so coverage and verification comparison stay complete."
)

#: MANUAL 결과의 점수. readiness 평균에서 제외되므로 이 값이 평균을 끌어내리지 않는다.
MANUAL_REVIEW_SCORE = 0.0


def governance_resource_id(repository_id: str) -> str:
    """The stable governance coordinate for one repository.

    Assessment ID를 쓰지 않는다 — Initial과 Verification이 같은 좌표를 가져야 비교가 성립한다.
    """
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise ValueError("repository_id must be a non-empty string")
    return f"governance:{repository_id}"


class ManualReviewEvaluator:
    """Produce the MANUAL_REVIEW result for one approved MANUAL Rule, calling nothing."""

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
        if rule.evaluation_type is not RuleEvaluationType.MANUAL:
            # 자동 평가 가능한 Rule을 사람 검토로 흘리면, 실제로 검사할 수 있었던 것이
            # 검사되지 않은 채 "검토 대기"로 남는다.
            raise ValueError("manual review evaluator only accepts MANUAL rules")
        if context.resource_type != GOVERNANCE_ASSESSMENT_RESOURCE_TYPE:
            raise ValueError("manual review evaluates the governance assessment resource only")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=EvaluationPerspective.MANUAL,
            status=EvaluationStatus.MANUAL_REVIEW,
            # severity는 Rule이 정한다. MANUAL이라고 낮추지 않는다 — 검토되지 않았다는 사실이
            # 그 통제의 중요도를 바꾸지는 않는다.
            severity=rule.severity.value,
            score=MANUAL_REVIEW_SCORE,
            rationale=MANUAL_REVIEW_RATIONALE,
            # 근거는 이 Rule이 인용한 정책 판본 그 자체다. 도구가 관찰한 것이 없으므로 그 외의
            # Evidence를 붙이면 관찰하지 않은 것을 관찰했다고 말하게 된다.
            evidence_references=tuple(
                reference.evidence_reference for reference in rule.source_references
            ),
            rule_version=rule.version,
            rubric_version=model_profile.rubric_version,
            model_profile_id=model_profile.model_profile_id,
            scoring_mode=ScoringMode.CONTINUOUS,
        )
