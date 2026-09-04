"""Deterministic, severity-weighted Initial Assessment readiness calculation.

**준비도는 status에서 나온다, score에서가 아니라.** 한 좌표가 준비도에 기여하는 값은
`STATUS_SCORES`(PASS 100, FAIL 0)다. 결과의 `score` 필드를 평균하지 않는 이유는 측정에 있다 —
모델은 72회 평가에서 0과 100만 냈고, 코드의 관측 비율은 분모가 리소스 개수라 미암호화 볼륨
하나라는 같은 위험이 볼륨을 더 붙일수록(1+1 → 50, 19+1 → 95) 점수를 올렸다. 두 엔진의 숫자는
단위가 달랐고, 한 평균에 들어가면 같은 위반이 어느 엔진을 지나갔느냐에 따라 75점 차이로
기여했다. status는 두 엔진이 같은 문언으로 내는 유일한 값이다.

**판정이 없는 좌표는 0점이 아니라 점수가 없다.** `INSUFFICIENT_EVIDENCE`와 `MANUAL_REVIEW`를
0으로 평균에 넣으면 "확인 못 함 1건 + 통과 1건"과 "위반 1건 + 통과 1건"이 같은 50.0이 된다.
이 서비스는 어디에 비용과 시간을 쓸지 알려주려고 존재하므로, 그 둘을 같은 숫자로 만드는 것은
제품 목적을 정면으로 거스른다. 그런 좌표는 `undetermined_evaluations`로 따로 센다.
"""

from collections.abc import Mapping, Sequence

from packages.contracts import (
    STATUS_SCORES,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
    ReadinessScore,
    SegmentReadinessScore,
)

_SEVERITY_WEIGHTS = {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 8}

#: 준비도에 아무 의미가 없는 status. OUT_OF_SCOPE는 그 Rule이 이 Resource를 규율하지 않는다는
#: 뜻이고, EXECUTION_ERROR는 Coverage가 게시 자체를 막는다. 둘 다 미판정으로도 세지 않는다.
_NON_SCORING_STATUSES = frozenset({EvaluationStatus.OUT_OF_SCOPE, EvaluationStatus.EXECUTION_ERROR})

#: 실행은 됐으나 판정이 없는 status. 평균에서 빼고 `undetermined_evaluations`로 보고한다.
_UNDETERMINED_STATUSES = frozenset(
    {EvaluationStatus.INSUFFICIENT_EVIDENCE, EvaluationStatus.MANUAL_REVIEW}
)

#: 숫자 readiness 평균에서 제외하는 Perspective. DRIFT는 두 Perspective의 비교이므로 평균에 넣으면
#: 같은 사실을 두 번 세는 것이고, MANUAL은 도구가 만든 판정이 아니라 사람이 정할 판단이다.
#: 두 Perspective 모두 Coverage와 plan 완료에는 그대로 포함되며, 미판정 수에도 세지 않는다 —
#: MANUAL 좌표는 정의상 항상 미판정이라 세면 정보가 아니다.
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

    Each judged coordinate contributes `STATUS_SCORES[status]` weighted by the policy
    Rule severity. OUT_OF_SCOPE has no readiness meaning and EXECUTION_ERROR prevents
    publication via Coverage. INSUFFICIENT_EVIDENCE and MANUAL_REVIEW are counted as
    undetermined rather than averaged in.

    `DRIFT` results are excluded from the score. Drift states whether the IaC and
    the AWS Actual perspective agree, not how well the resource satisfies the rule;
    folding its binary alignment value into the representative compliance score
    would raise readiness for a resource that is consistently non-compliant. Drift
    still reaches the user as its own results and Findings.

    Returns `None` when no coordinate was judged at all — a plan made only of
    undetermined coordinates has no readiness to publish, only a count of unknowns.
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
    candidates = tuple(
        result
        for result in results
        if result.status not in _NON_SCORING_STATUSES
        and result.perspective not in _NON_SCORING_PERSPECTIVES
    )
    judged = tuple(result for result in candidates if result.status in STATUS_SCORES)
    undetermined = tuple(result for result in candidates if result.status in _UNDETERMINED_STATUSES)
    if len(judged) + len(undetermined) != len(candidates):  # pragma: no cover - closed enum
        raise ValueError("result status is not supported for readiness scoring")
    if not judged:
        return None
    try:
        total_weight = sum(_SEVERITY_WEIGHTS[result.severity] for result in judged)
    except KeyError as error:
        raise ValueError("result severity is not supported for readiness scoring") from error
    weighted_score = sum(
        STATUS_SCORES[result.status] * _SEVERITY_WEIGHTS[result.severity] for result in judged
    )
    return ReadinessScore(
        score=round(weighted_score / total_weight, 2),
        evaluated_evaluations=len(judged),
        undetermined_evaluations=len(undetermined),
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
