"""Deterministic, severity-weighted Initial Assessment readiness calculation."""

from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ReadinessScore,
)

_SEVERITY_WEIGHTS = {"LOW": 1, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 8}
_NON_SCORING_STATUSES = frozenset({EvaluationStatus.OUT_OF_SCOPE, EvaluationStatus.EXECUTION_ERROR})


def calculate_readiness_score(
    *, results: tuple[EvaluationResult, ...], planned_evaluations: int
) -> ReadinessScore | None:
    """Return a score only when the immutable plan has fully completed.

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
    if isinstance(planned_evaluations, bool) or not isinstance(planned_evaluations, int):
        raise TypeError("planned_evaluations must be an integer")
    if planned_evaluations <= 0:
        raise ValueError("planned_evaluations must be greater than zero")
    completed = {
        (result.resource_id, result.rule_id, result.perspective.value)
        for result in results
        if isinstance(result, EvaluationResult)
        and result.status is not EvaluationStatus.EXECUTION_ERROR
    }
    if len(completed) != planned_evaluations:
        return None
    scoring_results = tuple(
        result
        for result in results
        if result.status not in _NON_SCORING_STATUSES
        and result.perspective is not EvaluationPerspective.DRIFT
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
