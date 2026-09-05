"""Assessment Coverage calculation over planned applicable evaluations."""

from packages.contracts import AssessmentCoverage, EvaluationResult


def calculate_coverage(
    *, results: tuple[EvaluationResult, ...], planned_evaluations: int
) -> AssessmentCoverage:
    """Count each recorded Resource × Rule × Perspective once.

    Coverage는 "실행됐는가"다. `EXECUTION_ERROR`도 runner가 사유까지 남긴 **기록된 결과**이므로
    완료로 센다 — 라이브 M1 경로의 transactional counter가 이미 그렇게 세어 화면이 146/146을
    보였는데, 이 fallback과 비교 경계만 실행 오류를 빼서 세 정의가 어긋나 있었다(2026-09-05까지).
    실패의 범위는 Coverage가 아니라 result status와 `ReadinessScore.errored_evaluations`가
    드러낸다. 같은 좌표의 재전송은 한 번만 센다.
    """
    if not isinstance(results, tuple):
        raise TypeError("results must be a tuple")
    completed_keys: set[tuple[str, str, str]] = set()
    for result in results:
        if not isinstance(result, EvaluationResult):
            raise TypeError("results must contain EvaluationResult values")
        completed_keys.add((result.resource_id, result.rule_id, result.perspective.value))
    return AssessmentCoverage(
        planned_evaluations=planned_evaluations,
        completed_evaluations=len(completed_keys),
    )
