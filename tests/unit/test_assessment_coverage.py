"""Coverage reports Assessment completion against the planned work set."""

import unittest

from apps.backend.assessment import AssessmentCoverage, calculate_coverage
from packages.contracts import EvaluationPerspective, EvaluationResult, EvaluationStatus


def result(
    *,
    resource_id: str = "bucket-001",
    rule_id: str = "S3-001",
    perspective: EvaluationPerspective = EvaluationPerspective.IAC,
    status: EvaluationStatus = EvaluationStatus.PASS,
) -> EvaluationResult:
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule_id,
        perspective=perspective,
        status=status,
        severity="HIGH",
        score=100,
        rationale="Fixture evaluation completed.",
        evidence_references=("fixture:evidence",),
        rule_version="v1",
        rubric_version="v1",
        model_profile_id="assessment-profile-v1",
    )


class AssessmentCoverageTest(unittest.TestCase):
    def test_uses_planned_applicable_evaluations_as_the_denominator(self) -> None:
        coverage = calculate_coverage(
            results=(
                result(),
                result(resource_id="bucket-002", status=EvaluationStatus.MANUAL_REVIEW),
            ),
            planned_evaluations=4,
        )

        self.assertEqual(coverage.completed_evaluations, 2)
        self.assertEqual(coverage.percentage, 50)
        self.assertEqual(
            coverage.to_dict(),
            {"planned_evaluations": 4, "completed_evaluations": 2, "percentage": 50},
        )

    def test_execution_error_remains_uncovered_and_duplicate_delivery_is_not_double_counted(
        self,
    ) -> None:
        outcome = result()
        coverage = calculate_coverage(
            results=(
                outcome,
                outcome,
                result(rule_id="S3-002", status=EvaluationStatus.EXECUTION_ERROR),
            ),
            planned_evaluations=2,
        )

        self.assertEqual(coverage.completed_evaluations, 1)
        self.assertEqual(coverage.percentage, 50)

    def test_rejects_a_result_set_larger_than_the_authoritative_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            calculate_coverage(
                results=(result(), result(resource_id="bucket-002")), planned_evaluations=1
            )

    def test_rejects_an_empty_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            AssessmentCoverage(planned_evaluations=0, completed_evaluations=0)
