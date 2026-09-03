"""비교 입력 reader 테스트 (ADR-0020 §3·§5).

고정하는 불변식:
- `model_profile_id`/`rubric_version`은 결과에서 파생한다. Initial Assessment에는 item pin이
  없으므로(그게 규칙이다) 양쪽을 같은 방법으로 읽어야 비교 축이 한 종류가 된다.
- 한 Assessment가 서로 다른 Profile/rubric을 섞고 있으면 비교 이전에 fail-closed한다.
- 부분 report(cursor가 남은 report)는 비교 입력이 되지 않는다.
- 검증 Assessment가 자기 자신을 원본으로 지목하면 거부한다.
"""

import unittest

from apps.backend.assessment.reporting import AssessmentReport
from apps.backend.repositories.comparison_input import DynamoDbComparisonInputReader
from apps.backend.repositories.ports import StoredDataError
from packages.contracts import (
    AssessmentCoverage,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
    ReadinessScore,
)

CUSTOMER_ID = "cust-001"
SOURCE_ID = "asm-source"
VERIFICATION_ID = "asm-verification"


def _result(
    *,
    resource_id: str = "bucket-001",
    rule_id: str = "S3-001",
    status: EvaluationStatus = EvaluationStatus.FAIL,
    rubric_version: str = "m1-v1",
    model_profile_id: str = "assessment-profile-v1",
) -> EvaluationResult:
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule_id,
        perspective=EvaluationPerspective.AWS_ACTUAL,
        status=status,
        severity="HIGH",
        score=100 if status is EvaluationStatus.PASS else 20,
        rationale="fixture",
        evidence_references=("aws:s3:fixture",),
        rule_version="v1",
        rubric_version=rubric_version,
        model_profile_id=model_profile_id,
    )


def _report(
    assessment_id: str,
    results: tuple[EvaluationResult, ...],
    *,
    next_cursor: str | None = None,
    planned: int | None = None,
) -> AssessmentReport:
    planned = len(results) if planned is None else planned
    return AssessmentReport(
        assessment_id=assessment_id,
        results=results,
        findings=(),
        coverage=AssessmentCoverage(
            planned_evaluations=planned, completed_evaluations=len(results)
        ),
        readiness_score=(
            ReadinessScore(score=20, evaluated_evaluations=len(results)) if results else None
        ),
        next_cursor=next_cursor,
    )


def _planned(results: tuple[EvaluationResult, ...]) -> tuple[PlannedEvaluation, ...]:
    return tuple(
        PlannedEvaluation(
            resource_id=item.resource_id, rule_id=item.rule_id, perspective=item.perspective
        )
        for item in results
    )


class FakeReports:
    def __init__(self, reports: dict[str, AssessmentReport]) -> None:
        self.reports = reports

    def get_report(self, *, customer_id: str, assessment_id: str) -> AssessmentReport:
        try:
            return self.reports[assessment_id]
        except KeyError:
            raise LookupError("assessment not found") from None

    def get_planned_evaluations(self, *, customer_id: str, assessment_id: str):
        return _planned(self.reports[assessment_id].results)


def _reader(**reports: AssessmentReport) -> DynamoDbComparisonInputReader:
    return DynamoDbComparisonInputReader(FakeReports(dict(reports)))


class ComparisonInputReaderTest(unittest.TestCase):
    def _default_reader(self) -> DynamoDbComparisonInputReader:
        return _reader(
            **{
                SOURCE_ID: _report(SOURCE_ID, (_result(),)),
                VERIFICATION_ID: _report(VERIFICATION_ID, (_result(status=EvaluationStatus.PASS),)),
            }
        )

    def _inputs(self, reader: DynamoDbComparisonInputReader):
        return reader.get_comparison_inputs(
            customer_id=CUSTOMER_ID,
            source_assessment_id=SOURCE_ID,
            verification_assessment_id=VERIFICATION_ID,
        )

    def test_derives_the_scope_from_the_results_of_each_assessment(self) -> None:
        """Initial Assessment에는 item pin이 없으므로 결과가 유일한 근거다."""
        source, verification = self._inputs(self._default_reader())
        self.assertEqual(source.assessment_id, SOURCE_ID)
        self.assertEqual(source.model_profile_id, "assessment-profile-v1")
        self.assertEqual(source.rubric_version, "m1-v1")
        self.assertEqual(verification.model_profile_id, "assessment-profile-v1")
        self.assertEqual(verification.rubric_version, "m1-v1")

    def test_carries_a_replaced_profile_through_so_the_comparison_can_reject_it(self) -> None:
        """Profile 교체는 숨기지 않는다 — 비교 경계가 `comparable=false`로 판정할 재료다."""
        reader = _reader(
            **{
                SOURCE_ID: _report(SOURCE_ID, (_result(),)),
                VERIFICATION_ID: _report(
                    VERIFICATION_ID, (_result(model_profile_id="assessment-profile-v2"),)
                ),
            }
        )
        source, verification = self._inputs(reader)
        self.assertNotEqual(source.model_profile_id, verification.model_profile_id)

    def test_mixed_scope_within_one_assessment_fails_closed(self) -> None:
        reader = _reader(
            **{
                SOURCE_ID: _report(
                    SOURCE_ID,
                    (_result(), _result(rule_id="S3-002", rubric_version="m1-v2")),
                ),
                VERIFICATION_ID: _report(VERIFICATION_ID, (_result(),)),
            }
        )
        with self.assertRaises(StoredDataError):
            self._inputs(reader)

    def test_a_partial_report_is_not_a_comparison_input(self) -> None:
        reader = _reader(
            **{
                SOURCE_ID: _report(SOURCE_ID, (_result(),), next_cursor="more"),
                VERIFICATION_ID: _report(VERIFICATION_ID, (_result(),)),
            }
        )
        with self.assertRaises(StoredDataError):
            self._inputs(reader)

    def test_an_assessment_without_results_fails_closed(self) -> None:
        """평가가 아직 하나도 기록되지 않았으면 쓰인 Profile/rubric을 알 수 없다."""
        reader = _reader(
            **{
                SOURCE_ID: _report(SOURCE_ID, (), planned=1),
                VERIFICATION_ID: _report(VERIFICATION_ID, (_result(),)),
            }
        )
        with self.assertRaises(StoredDataError):
            self._inputs(reader)

    def test_a_verification_cannot_be_its_own_source(self) -> None:
        with self.assertRaises(StoredDataError):
            self._default_reader().get_comparison_inputs(
                customer_id=CUSTOMER_ID,
                source_assessment_id=SOURCE_ID,
                verification_assessment_id=SOURCE_ID,
            )

    def test_requires_non_empty_identifiers(self) -> None:
        with self.assertRaises(ValueError):
            self._default_reader().get_comparison_inputs(
                customer_id="",
                source_assessment_id=SOURCE_ID,
                verification_assessment_id=VERIFICATION_ID,
            )


if __name__ == "__main__":
    unittest.main()
