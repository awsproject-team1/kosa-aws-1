"""Assessment Coverage calculation over planned applicable evaluations."""

from packages.contracts import AssessmentCoverage, EvaluationResult, EvaluationStatus


def calculate_coverage(
    *, results: tuple[EvaluationResult, ...], planned_evaluations: int
) -> AssessmentCoverage:
    """Count each successfully evaluated Resource × Rule × Perspective once.

    `EXECUTION_ERROR` means no usable evaluation was completed, so it remains in
    the planned denominator but does not increase coverage. Every other validated
    status represents a completed assessment outcome, including MANUAL_REVIEW and
    INSUFFICIENT_EVIDENCE.
    """
    if not isinstance(results, tuple):
        raise TypeError("results must be a tuple")
    completed_keys: set[tuple[str, str, str]] = set()
    for result in results:
        if not isinstance(result, EvaluationResult):
            raise TypeError("results must contain EvaluationResult values")
        if result.status is not EvaluationStatus.EXECUTION_ERROR:
            completed_keys.add((result.resource_id, result.rule_id, result.perspective.value))
    return AssessmentCoverage(
        planned_evaluations=planned_evaluations,
        completed_evaluations=len(completed_keys),
    )
