"""A-owned reader assembling the two complete Assessments a comparison needs (ADR-0020).

`GET /deployments/{id}/verification`은 원 Assessment와 검증 Assessment를 각각 complete
`ComparisonAssessment`로 만들어 `compare_post_deploy_assessments()`에 넘긴다. C의 비교 경계는
부분 report(cursor가 남은 report)를 fail-closed로 거부하므로, 이 reader는 완결된 report만 만든다.

`model_profile_id`/`rubric_version`은 **결과에서 파생한다**. 두 값은 모든
`ASSESSMENT#...#RESULT#...` item에 이미 들어 있고(`EvaluationResult`의 필수 필드), 그게 실제로
평가에 쓰인 값이다. Assessment item의 pin을 대신 읽지 않는 이유는 둘이다:

1. Initial Assessment에는 그 pin이 **없다**. pin은 검증 Assessment 전용이고, 없는 것이 규칙이다
   (`DATABASE.md` M3 storage, ADR-0020 §3). 그래서 원 Assessment 쪽은 파생 말고는 방법이 없고,
   양쪽을 같은 방법으로 읽어야 비교 축이 한 종류가 된다.
2. pin은 "이 값으로 평가하라"는 사전 조건이고 결과의 값은 "이 값으로 평가했다"는 사실이다.
   비교가 필요로 하는 건 후자다. 둘이 어긋나는 경우는 Worker runtime이 이미 거부한다
   (`apps/backend/assessment/runtime.py`의 저장된 pin 대조).

한 Assessment의 결과가 서로 다른 Profile/rubric을 담고 있으면 그 Assessment 자체가 한 축에
놓이지 않으므로 비교 이전에 fail-closed한다.
"""

from typing import Protocol

from apps.backend.assessment.comparison import ComparisonAssessment
from apps.backend.assessment.reporting import AssessmentReport
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import PlannedEvaluation


class AssessmentReportReader(Protocol):
    def get_report(self, *, customer_id: str, assessment_id: str) -> AssessmentReport: ...

    def get_planned_evaluations(
        self, *, customer_id: str, assessment_id: str
    ) -> tuple[PlannedEvaluation, ...]: ...


class DynamoDbComparisonInputReader:
    """Load the source and verification Assessments as complete comparison inputs."""

    def __init__(self, reports: AssessmentReportReader) -> None:
        if reports is None:
            raise TypeError("reports reader is required")
        self._reports = reports

    def get_comparison_inputs(
        self, *, customer_id: str, source_assessment_id: str, verification_assessment_id: str
    ) -> tuple[ComparisonAssessment, ComparisonAssessment]:
        for value, name in (
            (customer_id, "customer_id"),
            (source_assessment_id, "source_assessment_id"),
            (verification_assessment_id, "verification_assessment_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if source_assessment_id == verification_assessment_id:
            # A verification that names itself as its own source would compare a report
            # with itself and always report "resolved nothing changed".
            raise StoredDataError("verification Assessment cannot be its own source")
        return (
            self._comparison_assessment(customer_id, source_assessment_id),
            self._comparison_assessment(customer_id, verification_assessment_id),
        )

    def _comparison_assessment(self, customer_id: str, assessment_id: str) -> ComparisonAssessment:
        report = self._reports.get_report(customer_id=customer_id, assessment_id=assessment_id)
        if not isinstance(report, AssessmentReport):
            raise StoredDataError("assessment report is invalid")
        planned = self._reports.get_planned_evaluations(
            customer_id=customer_id, assessment_id=assessment_id
        )
        model_profile_id, rubric_version = _observed_scope(report, assessment_id)
        try:
            return ComparisonAssessment(
                assessment_id=assessment_id,
                model_profile_id=model_profile_id,
                rubric_version=rubric_version,
                planned_evaluations=planned,
                report=report,
            )
        except (TypeError, ValueError) as error:
            # Incomplete or inconsistent stored input, not a caller error: the
            # comparison contract itself rejects partial reports (ADR-0020 §5).
            raise StoredDataError(
                f"stored assessment {assessment_id} is not a complete comparison input"
            ) from error


def _observed_scope(report: AssessmentReport, assessment_id: str) -> tuple[str, str]:
    """Return the single Model Profile and rubric every result of one Assessment used."""
    if not report.results:
        raise StoredDataError(f"assessment {assessment_id} has no results to compare")
    profiles = {result.model_profile_id for result in report.results}
    rubrics = {result.rubric_version for result in report.results}
    if len(profiles) != 1 or len(rubrics) != 1:
        raise StoredDataError(f"assessment {assessment_id} mixes Model Profiles or rubric versions")
    return profiles.pop(), rubrics.pop()
