"""Deterministic, severity-weighted Initial Assessment readiness calculation."""

from collections.abc import Mapping, Sequence

from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
    ReadinessScore,
    SegmentReadinessScore,
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


def calculate_segment_readiness(
    *,
    results: tuple[EvaluationResult, ...],
    planned_evaluations: tuple[PlannedEvaluation, ...],
    rule_kinds: Mapping[str, Sequence[str]],
) -> tuple[SegmentReadinessScore, ...]:
    """Score each policy origin the Profile spans, separately.

    사내 정책과 ISMS-P를 한 Profile에 담을 수 있게 되면서 필요해진 계산이다. 합친 하나의 숫자는
    어느 기준에 대한 답도 아니므로, 원본 종류별로 그 원본이 뒷받침하는 좌표만 모아 같은
    severity 가중 평균을 낸다.

    **한 Rule이 여러 원본에 속할 수 있다.** 기준선 Rule 대부분이 사내 체크리스트와 ISMS-P 조항을
    함께 인용하며, 그런 Rule은 두 준비도 모두에 들어간다. 중복 계산이 아니라, 그 Rule이 실제로
    두 기준을 동시에 뒷받침한다는 사실이다.

    각 원본의 점수는 **그 원본의 계획이 전부 끝났을 때만** 나온다(`calculate_readiness_score`와
    같은 규칙). 한쪽이 아직 진행 중이면 그쪽만 `None`이고 다른 쪽은 그대로 나온다 — 하나의
    전체 점수로 묶여 있을 때는 할 수 없던 구분이다.

    `rule_kinds`가 비어 있으면 빈 tuple을 돌려준다. 원본을 분류하지 않고 게시된 Profile
    (이 계약 이전의 모든 Profile)은 나눌 근거가 없으므로 지금처럼 전체 점수 하나만 갖는다.
    """
    if not isinstance(results, tuple):
        raise TypeError("results must be a tuple")
    if not isinstance(planned_evaluations, tuple):
        raise TypeError("planned_evaluations must be a tuple")
    if not isinstance(rule_kinds, Mapping):
        raise TypeError("rule_kinds must be a mapping")
    kinds: list[str] = []
    for values in rule_kinds.values():
        if isinstance(values, str):
            raise TypeError("rule_kinds values must be sequences of kind strings")
        for kind in values:
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError("rule_kinds values must be non-empty strings")
            if kind not in kinds:
                kinds.append(kind)
    if not kinds:
        return ()
    scores: list[SegmentReadinessScore] = []
    for kind in kinds:
        rule_ids = {rule_id for rule_id, values in rule_kinds.items() if kind in values}
        planned = tuple(
            coordinate for coordinate in planned_evaluations if coordinate.rule_id in rule_ids
        )
        if not planned:
            # 계획에 없는 원본이다. Rule은 Profile에 있으나 어떤 Resource에도 적용되지 않았다.
            scores.append(SegmentReadinessScore(kind=kind, score=None))
            continue
        scores.append(
            SegmentReadinessScore(
                kind=kind,
                score=calculate_readiness_score(
                    results=tuple(result for result in results if result.rule_id in rule_ids),
                    planned_evaluations=planned,
                ),
            )
        )
    return tuple(scores)
