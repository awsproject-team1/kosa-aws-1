"""Validate evaluator output against the approved Policy Context before persistence.

**한 좌표의 실패가 나머지를 버리지 않는다 (2026-09-05).** 예전에는 평가기가 던진 예외가 그대로
올라가 Worker 실행 전체를 죽였다. 그러면 이미 끝난 17개 좌표의 결과도 저장되지 않고, SQS가 같은
작업을 재전달하며, 두 번째 실행이 다른 답을 내면 immutable 저장소가 `ImmutableEvaluationResultConflict`로
거부해 결국 DLQ로 간다 — 라이브에서 48시간 동안 38,899회 실패가 그 고리였다.

그래서 실패한 좌표는 그 좌표의 `EXECUTION_ERROR` 결과가 된다. 이것은 관대해지는 것이 아니다:
`EXECUTION_ERROR`는 Coverage에서 완료로 세지 않고(`calculate_coverage`), readiness 게시를 막으며
(`calculate_readiness_score`), Finding이 되지도 않는다. 실패는 조용해지는 것이 아니라 **보이는
미완료**로 남고, 나머지 평가는 살아남는다.
"""

from typing import Protocol

from apps.backend.policy import PolicyContext
from packages.contracts import (
    DecisionSource,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    PolicyRule,
    ScoringMode,
    score_for_status,
)


class EvaluationContractError(ValueError):
    """Raised when an evaluator returns a result outside the approved rule context."""


class Evaluator(Protocol):
    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult: ...


class AssessmentRunner:
    """Run one approved Resource × Rule evaluation set using an injected model adapter."""

    def __init__(
        self, evaluator: Evaluator, *, perspective: EvaluationPerspective | None = None
    ) -> None:
        if evaluator is None:
            raise TypeError("evaluator is required")
        if perspective is not None and not isinstance(perspective, EvaluationPerspective):
            raise TypeError("perspective must be an EvaluationPerspective")
        self._evaluator = evaluator
        # 실패한 좌표의 결과에도 Perspective가 필요한데, 그 값은 평가기가 갖는다. 명시되지 않으면
        # 평가기에게 묻고(어댑터들이 이미 그 속성을 갖는다), 그마저 없으면 실패를 그대로 올린다 —
        # 어느 관점의 실패인지 말할 수 없는 결과를 지어내지 않는다.
        self._perspective = perspective

    def evaluate_resource(
        self, *, resource_id: str, context: PolicyContext, model_profile: ModelProfile
    ) -> tuple[EvaluationResult, ...]:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if not isinstance(context, PolicyContext):
            raise TypeError("context must be a PolicyContext")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        results: list[EvaluationResult] = []
        for rule in context.rules:
            try:
                results.append(
                    self._validated_result(
                        self._evaluator.evaluate(
                            resource_id=resource_id,
                            rule=rule,
                            context=context,
                            model_profile=model_profile,
                        ),
                        resource_id=resource_id,
                        rule_id=rule.rule_id,
                        rule_version=rule.version,
                        context=context,
                    )
                )
            except (EvaluationContractError, ValueError) as error:
                # 평가기가 답을 만들지 못했다. 그 사실을 이 좌표에만 기록하고 나머지를 계속한다.
                # `TypeError`는 여기서 잡지 않는다 — 그것은 배선 오류이지 평가 실패가 아니다.
                perspective = self._failed_perspective()
                if perspective is None:
                    raise
                results.append(
                    _execution_error(
                        resource_id=resource_id,
                        rule=rule,
                        perspective=perspective,
                        model_profile=model_profile,
                        error=error,
                    )
                )
        return tuple(results)

    def _failed_perspective(self) -> EvaluationPerspective | None:
        if self._perspective is not None:
            return self._perspective
        perspective = getattr(self._evaluator, "perspective", None)
        return perspective if isinstance(perspective, EvaluationPerspective) else None

    @staticmethod
    def _validated_result(
        result: EvaluationResult,
        *,
        resource_id: str,
        rule_id: str,
        rule_version: str,
        context: PolicyContext,
    ) -> EvaluationResult:
        if not isinstance(result, EvaluationResult):
            raise EvaluationContractError("evaluator must return an EvaluationResult")
        if result.resource_id != resource_id:
            raise EvaluationContractError("evaluator result resource_id is outside request context")
        if result.rule_id != rule_id or result.rule_version != rule_version:
            raise EvaluationContractError(
                "evaluator result rule is outside approved policy context"
            )
        # 정책 근거는 이 Context의 canonical SourceReference여야 하고, Resource 상태 근거는
        # 허용된 namespace여야 한다. 그렇지 않으면 평가기가 승인 범위 밖 근거를 만든 것이다.
        for reference in result.evidence_references:
            if not context.allows_evidence(reference):
                raise EvaluationContractError(
                    "evaluator result cites evidence outside the approved policy context"
                )
        return result


def _execution_error(
    *,
    resource_id: str,
    rule: PolicyRule,
    perspective: EvaluationPerspective,
    model_profile: ModelProfile,
    error: Exception,
) -> EvaluationResult:
    """Record that this coordinate could not be evaluated, without claiming anything about it.

    rationale에는 예외의 **종류**만 담는다. 예외 문구에는 모델이 지어낸 locator처럼 응답에서 온
    문자열이 들어 있고, 그것을 결과에 실으면 거부한 값을 그대로 저장하는 셈이다. 근거는 비운다 —
    관찰한 것이 없다.
    """
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule.rule_id,
        perspective=perspective,
        status=EvaluationStatus.EXECUTION_ERROR,
        severity=rule.severity.value,
        score=score_for_status(EvaluationStatus.EXECUTION_ERROR),
        rationale=(
            f"The evaluation did not complete: {type(error).__name__}. "
            "This coordinate is not counted as covered and no judgment is recorded for it."
        ),
        evidence_references=(),
        rule_version=rule.version,
        rubric_version=model_profile.rubric_version,
        model_profile_id=model_profile.model_profile_id,
        scoring_mode=ScoringMode.CONTINUOUS,
        decided_by=DecisionSource.CODE,
    )
