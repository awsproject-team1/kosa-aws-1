"""Readiness publication is gated by the planned set, not by a count (ADR-0020)."""

import unittest

from apps.backend.assessment import calculate_readiness_score
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PlannedEvaluation,
)


def planned(
    *,
    resource_id: str = "bucket-001",
    rule_id: str = "S3-001",
    perspective: EvaluationPerspective = EvaluationPerspective.IAC,
) -> PlannedEvaluation:
    return PlannedEvaluation(resource_id=resource_id, rule_id=rule_id, perspective=perspective)


def result(
    *,
    resource_id: str = "bucket-001",
    rule_id: str = "S3-001",
    perspective: EvaluationPerspective = EvaluationPerspective.IAC,
    status: EvaluationStatus = EvaluationStatus.PASS,
    score: float = 100,
    severity: str = "HIGH",
) -> EvaluationResult:
    return EvaluationResult(
        resource_id=resource_id,
        rule_id=rule_id,
        perspective=perspective,
        status=status,
        severity=severity,
        score=score,
        rationale="Fixture result.",
        evidence_references=("fixture:evidence",),
        rule_version="v1",
        rubric_version="v1",
        model_profile_id="assessment-profile-v1",
    )


class ReadinessScoreTest(unittest.TestCase):
    def test_scores_a_plan_only_when_every_planned_coordinate_completed(self) -> None:
        score = calculate_readiness_score(
            results=(result(score=20), result(resource_id="bucket-002", score=100, severity="LOW")),
            planned_evaluations=(planned(), planned(resource_id="bucket-002")),
        )

        assert score is not None
        self.assertEqual(score.score, 36)
        self.assertEqual(score.evaluated_evaluations, 2)

    def test_an_unplanned_result_does_not_fill_a_missing_planned_coordinate(self) -> None:
        """The defect a count comparison cannot see: the totals agree, the plan does not."""
        score = calculate_readiness_score(
            results=(result(), result(resource_id="bucket-003")),
            planned_evaluations=(planned(), planned(resource_id="bucket-002")),
        )

        self.assertIsNone(score)

    def test_incomplete_plan_has_no_score(self) -> None:
        self.assertIsNone(
            calculate_readiness_score(
                results=(result(),),
                planned_evaluations=(planned(), planned(resource_id="bucket-002")),
            )
        )

    def test_execution_error_leaves_its_coordinate_uncompleted(self) -> None:
        self.assertIsNone(
            calculate_readiness_score(
                results=(
                    result(),
                    result(resource_id="bucket-002", status=EvaluationStatus.EXECUTION_ERROR),
                ),
                planned_evaluations=(planned(), planned(resource_id="bucket-002")),
            )
        )

    def test_out_of_scope_completes_its_coordinate_but_does_not_score(self) -> None:
        """A vanished resource keeps Coverage whole while staying out of the score."""
        score = calculate_readiness_score(
            results=(
                result(score=40),
                result(resource_id="bucket-002", status=EvaluationStatus.OUT_OF_SCOPE, score=0),
            ),
            planned_evaluations=(planned(), planned(resource_id="bucket-002")),
        )

        assert score is not None
        self.assertEqual(score.score, 40)
        self.assertEqual(score.evaluated_evaluations, 1)

    def test_drift_completes_its_coordinate_but_is_excluded_from_the_score(self) -> None:
        score = calculate_readiness_score(
            results=(
                result(score=40),
                result(perspective=EvaluationPerspective.DRIFT, score=0),
            ),
            planned_evaluations=(
                planned(),
                planned(perspective=EvaluationPerspective.DRIFT),
            ),
        )

        assert score is not None
        self.assertEqual(score.score, 40)
        self.assertEqual(score.evaluated_evaluations, 1)

    def test_rejects_a_duplicated_or_empty_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            calculate_readiness_score(results=(), planned_evaluations=(planned(), planned()))
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            calculate_readiness_score(results=(), planned_evaluations=())

    def test_rejects_a_count_where_the_planned_set_is_required(self) -> None:
        with self.assertRaises(TypeError):
            calculate_readiness_score(results=(result(),), planned_evaluations=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
