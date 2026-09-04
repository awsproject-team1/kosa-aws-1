"""Settle the Actual perspective from declared evidence when the criterion is a fact, not a judgment.

**왜.** 이 서비스는 ISMS-P 준비도 평가의 비용과 시간을 줄이기 위해 존재한다. 그런데 "네 플래그가
모두 켜져 있는가", "HTTP 리스너가 있는가" 같은 물음은 판단이 아니라 사실이다. 사실을 모델에게
물으면 세 가지를 한꺼번에 잃는다.

- **정확도.** 라이브 측정에서 status 오류 3건이 전부 이 부류였고, 셋 다 위반을 PASS로 본 false
  negative였다(부분 준수 Case 4건 중 2건 오답). 준법 제품에서 false negative는 "준비됐다"고 말해
  놓고 준비되지 않은 상태이므로 가장 나쁜 실패다.
- **점수.** 모델은 등급을 매기지 않았다 — 72회 평가에서 score는 0과 100뿐이었다. 반면 "선언된 네
  경로 중 둘이 충족" 같은 비율은 계산이므로 그대로 연속 점수가 된다.
- **비용과 지연.** 판정 하나가 Bedrock 왕복 하나다.

**무엇을 대체하지 않는가.** 이것은 두 번째 평가 엔진이 아니다. 같은 `AssessmentRunner` 뒤에서
같은 `EvaluationResult`를 만들고, 같은 Catalog·같은 Rule·같은 근거 문서를 쓴다. 술어가 선언되지
않은 capability는 지금처럼 모델이 판단한다(`ActualBedrockEvaluator`가 그대로 위임한다). 해석이
필요한 통제 — 조직 통제, "적절한 범위로 제한" 같은 문언 — 는 여전히 모델과 사람의 몫이다.

**점수 규칙.** status는 Rule 문언 그대로다: 선언된 술어를 **모두** 충족해야 PASS다. score는
충족한 관측치의 비율이며, 임계값이나 Anchor가 아니라 측정된 비율이다(ADR-0003의 연속 점수 정책).
관측치가 하나뿐인 술어는 자연히 0 또는 100이 된다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apps.backend.policy.evidence_paths import document_path_values
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    EvidenceCapabilityBinding,
    EvidenceExpectation,
    GovernanceControlCatalog,
    ModelProfile,
    PolicyRule,
    ScoringMode,
)


class DeterministicEvaluationError(ValueError):
    """Raised when a declared predicate cannot be applied to the document it names."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _Observation:
    """One value the predicate judged, and whether it satisfied the criterion."""

    path: str
    satisfied: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DeterministicVerdict:
    """The decided outcome, carrying enough to explain itself to a reviewer."""

    status: EvaluationStatus
    score: float
    rationale: str

    @property
    def is_pass(self) -> bool:
        return self.status is EvaluationStatus.PASS


def decidable_bindings(
    catalog: GovernanceControlCatalog, rule: PolicyRule, *, resource_type: str
) -> tuple[EvidenceCapabilityBinding, ...]:
    """The AWS bindings that can settle this Rule, or `()` when any of them cannot.

    Rule이 요구한 capability 중 **하나라도** 술어가 없으면 결정적으로 답할 수 없다. 일부만
    코드가 판정하고 나머지를 모델이 판정하면 하나의 결과가 두 근거 체계를 섞게 되므로, 그 경우는
    통째로 모델에 맡긴다.
    """
    if rule.evaluation_type is None or not rule.required_evidence or not rule.control_key:
        return ()
    control = catalog.control(rule.control_key)
    if control is None:
        return ()
    bindings = {
        binding.capability_key: binding
        for binding in control.available_evidence_capabilities
        if binding.perspective is EvaluationPerspective.AWS_ACTUAL
        and binding.resource_type == resource_type
    }
    selected: list[EvidenceCapabilityBinding] = []
    for capability_key in rule.required_evidence:
        binding = bindings.get(capability_key)
        if binding is None or not binding.is_decidable:
            return ()
        selected.append(binding)
    return tuple(selected)


def decide(
    bindings: tuple[EvidenceCapabilityBinding, ...], document: Mapping[str, object]
) -> DeterministicVerdict:
    """Apply every declared predicate to the document and report the combined outcome."""
    if not bindings:
        raise DeterministicEvaluationError("a deterministic verdict requires at least one binding")
    observations: list[_Observation] = []
    for binding in bindings:
        for path in binding.judged_paths:
            values = document_path_values(document, path)
            if not values:
                # pre-flight가 경로의 존재를 보장한 뒤에 호출되므로 여기서 비면 문서 모양이
                # 선언과 어긋난 것이다. 조용히 통과시키지 않는다.
                raise DeterministicEvaluationError(
                    f"declared evidence path carries no value: {path}"
                )
            for value in values:
                observations.append(_Observation(path=path, satisfied=_satisfies(binding, value)))
    satisfied = [observation for observation in observations if observation.satisfied]
    unsatisfied = [observation for observation in observations if not observation.satisfied]
    score = round(len(satisfied) / len(observations) * 100, 2)
    if not unsatisfied:
        return DeterministicVerdict(
            status=EvaluationStatus.PASS,
            score=score,
            rationale=(
                "Every declared evidence path satisfies the control criterion: "
                + ", ".join(sorted({observation.path for observation in observations}))
                + "."
            ),
        )
    failing = sorted({observation.path for observation in unsatisfied})
    return DeterministicVerdict(
        status=EvaluationStatus.FAIL,
        score=score,
        rationale=(
            f"{len(satisfied)} of {len(observations)} declared evidence observations satisfy the "
            "control criterion. These do not: " + ", ".join(failing) + "."
        ),
    )


def _satisfies(binding: EvidenceCapabilityBinding, value: object) -> bool:
    """Whether one observed value meets the binding's declared expectation.

    비교는 좁게 한다. `ALL_TRUE`는 참으로 **평가되는** 값이 아니라 boolean `True`만 받는다 —
    `"vol-0abc"`가 참으로 취급되면 식별자가 통제를 통과시킨다. 문자열 비교는 AWS attribute가
    `"true"`/`"false"` 문자열로 오는 경우를 위해 대소문자를 무시한다.
    """
    expectation = binding.expectation
    if expectation is EvidenceExpectation.ALL_TRUE:
        return value is True
    if expectation is EvidenceExpectation.ALL_FALSE:
        return value is False
    if expectation is EvidenceExpectation.NON_EMPTY:
        if isinstance(value, (str, bytes, list, tuple, dict, set)):
            return len(value) > 0
        return value is not None
    if expectation in (EvidenceExpectation.ALL_EQUAL, EvidenceExpectation.NONE_EQUAL):
        matches = (
            isinstance(value, str) and value.casefold() == (binding.expected_value or "").casefold()
        )
        return matches if expectation is EvidenceExpectation.ALL_EQUAL else not matches
    raise DeterministicEvaluationError(f"unsupported expectation {expectation!r}")


def result_from_verdict(
    verdict: DeterministicVerdict,
    *,
    resource_id: str,
    rule: PolicyRule,
    evidence_references: tuple[str, ...],
    model_profile: ModelProfile,
) -> EvaluationResult:
    """Build the Assessment result, shaped exactly like any other Actual result.

    `model_profile_id`와 `rubric_version`은 그대로 싣는다 — 모델을 부르지 않았더라도 이 결과가
    어떤 승인된 평가 구성에서 나왔는지는 같은 방식으로 복원돼야 하고, 배포 전후 비교가 그 두 값의
    일치를 요구한다(`ManualReviewEvaluator`도 같은 규약을 따른다).
    """
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule.rule_id,
        perspective=EvaluationPerspective.AWS_ACTUAL,
        status=verdict.status,
        severity=rule.severity.value,
        score=verdict.score,
        rationale=verdict.rationale,
        # 관찰한 것은 AWS read 하나이고, 그 판정의 근거는 Rule이 인용한 정책 판본이다.
        evidence_references=tuple(
            dict.fromkeys(
                (
                    *evidence_references,
                    *(reference.evidence_reference for reference in rule.source_references),
                )
            )
        ),
        rule_version=rule.version,
        rubric_version=model_profile.rubric_version,
        model_profile_id=model_profile.model_profile_id,
        scoring_mode=ScoringMode.CONTINUOUS,
    )
