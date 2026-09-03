"""Deterministic, severity-weighted Initial Assessment readiness calculation."""

from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
    ReadinessScore,
)

_SEVERITY_WEIGHTS = {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 8}
_NON_SCORING_STATUSES = frozenset({EvaluationStatus.OUT_OF_SCOPE, EvaluationStatus.EXECUTION_ERROR})

#: 숫자 readiness 평균에서 제외하는 Perspective. **status 기준은 건드리지 않는다** — 기존
#: IAC/AWS_ACTUAL 결과의 `MANUAL_REVIEW`는 지금처럼 점수에 들어간다.
#:
#: DRIFT는 두 Perspective의 비교이므로 평균에 넣으면 같은 사실을 두 번 세는 것이고, MANUAL은
#: 도구가 만든 점수가 아니라 사람이 정할 판단이므로 0점이 평균을 끌어내리면 그 숫자는 "아직
#: 검토되지 않았다"가 아니라 "위반이 있다"로 읽힌다. 두 Perspective 모두 Coverage와 plan
#: 완료에는 그대로 포함된다.
_NON_SCORING_PERSPECTIVES = frozenset({EvaluationPerspective.DRIFT, EvaluationPerspective.MANUAL})


def calculate_readiness_score(
    *,
    results: tuple[EvaluationResult, ...],
    planned_evaluations: tuple[PlannedEvaluation, ...],
) -> ReadinessScore | None:
    """Return a score only when the immutable plan has fully completed.

    Completion is a set comparison against the planned coordinates, not a count
    (ADR-0020 §5). Counting alone publishes a score when an unplanned evaluation
    silently fills the slot of a planned one that never ran.

    Evaluation scores are weighted by the policy Rule severity. OUT_OF_SCOPE has
    no readiness meaning and EXECUTION_ERROR prevents publication via Coverage.

    `DRIFT` results are excluded from the score. Drift states whether the IaC and
    the AWS Actual perspective agree, not how well the resource satisfies the rule;
    folding its binary alignment value into the representative compliance score
    would raise readiness for a resource that is consistently non-compliant. Drift
    still reaches the user as its own results and Findings.
    """
    if not isinstance(results, tuple):
        raise TypeError("results must be a tuple")
    if not isinstance(planned_evaluations, tuple):
        raise TypeError("planned_evaluations must be a tuple")
    if not all(isinstance(value, PlannedEvaluation) for value in planned_evaluations):
        raise TypeError("planned_evaluations must contain PlannedEvaluation values")
    planned = set(planned_evaluations)
    if not planned:
        raise ValueError("planned_evaluations must not be empty")
    if len(planned) != len(planned_evaluations):
        raise ValueError("planned_evaluations must not contain duplicates")
    completed = {
        PlannedEvaluation(
            resource_id=result.resource_id,
            rule_id=result.rule_id,
            perspective=result.perspective,
        )
        for result in results
        if isinstance(result, EvaluationResult)
        and result.status is not EvaluationStatus.EXECUTION_ERROR
    }
    if completed != planned:
        return None
    scoring_results = tuple(
        result
        for result in results
        if result.status not in _NON_SCORING_STATUSES
        and result.perspective not in _NON_SCORING_PERSPECTIVES
    )
    if not scoring_results:
        return None
    try:
        total_weight = sum(_SEVERITY_WEIGHTS[result.severity] for result in scoring_results)
    except KeyError as error:
        raise ValueError("result severity is not supported for readiness scoring") from error
    weighted_score = sum(
        result.score * _SEVERITY_WEIGHTS[result.severity] for result in scoring_results
    )
    return ReadinessScore(
        score=round(weighted_score / total_weight, 2),
        evaluated_evaluations=len(scoring_results),
    )
