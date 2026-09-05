"""Readiness publication is gated by the planned set, not by a count (ADR-0020)."""

import unittest

from apps.backend.assessment import calculate_readiness_score
from apps.backend.assessment.readiness import calculate_segment_readiness
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
            results=(
                result(status=EvaluationStatus.FAIL, score=0),
                result(resource_id="bucket-002", score=100, severity="LOW"),
            ),
            planned_evaluations=(planned(), planned(resource_id="bucket-002")),
        )

        assert score is not None
        # HIGH FAIL(0 × 4) + LOW PASS(100 × 1) / 5
        self.assertEqual(score.score, 20)
        self.assertEqual(score.evaluated_evaluations, 2)
        self.assertEqual(score.undetermined_evaluations, 0)

    def test_the_status_decides_the_contribution_not_the_score_field(self) -> None:
        """모델은 0과 100만 냈고 코드의 비율은 분모가 리소스 개수였다. status만이 공통 문언이다."""
        score = calculate_readiness_score(
            results=(result(status=EvaluationStatus.PASS, score=20),),
            planned_evaluations=(planned(),),
        )

        assert score is not None
        self.assertEqual(score.score, 100)

    def test_undetermined_coordinates_are_counted_not_averaged(self) -> None:
        """ "확인 못 함 + 통과"와 "위반 + 통과"가 같은 숫자였다. 이제 전자는 100에 미판정 1이다."""
        for status in (EvaluationStatus.INSUFFICIENT_EVIDENCE, EvaluationStatus.MANUAL_REVIEW):
            with self.subTest(status=status):
                score = calculate_readiness_score(
                    results=(
                        result(status=status, score=0),
                        result(resource_id="bucket-002"),
                    ),
                    planned_evaluations=(planned(), planned(resource_id="bucket-002")),
                )

                assert score is not None
                self.assertEqual(score.score, 100)
                self.assertEqual(score.evaluated_evaluations, 1)
                self.assertEqual(score.undetermined_evaluations, 1)

    def test_a_plan_with_only_undetermined_coordinates_has_no_score(self) -> None:
        self.assertIsNone(
            calculate_readiness_score(
                results=(result(status=EvaluationStatus.INSUFFICIENT_EVIDENCE, score=0),),
                planned_evaluations=(planned(),),
            )
        )

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

    def test_execution_error_completes_its_coordinate_and_is_counted_not_scored(self) -> None:
        """실행 오류는 기록된 결과다. 점수를 막지 않고, 평균에서 빠지며, 수가 점수 옆에 실린다.

        예전에는 실행 오류 하나가 판정된 좌표 전부의 점수를 숨겼다(계산 불가) — Coverage는 같은
        좌표를 실행됨으로 세면서. 누락 좌표(결과 없음)만 점수를 막는다.
        """
        score = calculate_readiness_score(
            results=(
                result(),
                result(resource_id="bucket-002", status=EvaluationStatus.EXECUTION_ERROR),
            ),
            planned_evaluations=(planned(), planned(resource_id="bucket-002")),
        )

        assert score is not None
        self.assertEqual(score.evaluated_evaluations, 1)
        self.assertEqual(score.errored_evaluations, 1)
        self.assertEqual(score.undetermined_evaluations, 0)
        self.assertEqual(score.to_dict()["errored_evaluations"], 1)

    def test_a_plan_of_only_execution_errors_has_no_score(self) -> None:
        self.assertIsNone(
            calculate_readiness_score(
                results=(result(status=EvaluationStatus.EXECUTION_ERROR),),
                planned_evaluations=(planned(),),
            )
        )

    def test_out_of_scope_completes_its_coordinate_but_does_not_score(self) -> None:
        """A vanished resource keeps Coverage whole while staying out of the score."""
        score = calculate_readiness_score(
            results=(
                result(),
                result(resource_id="bucket-002", status=EvaluationStatus.OUT_OF_SCOPE, score=0),
            ),
            planned_evaluations=(planned(), planned(resource_id="bucket-002")),
        )

        assert score is not None
        self.assertEqual(score.score, 100)
        self.assertEqual(score.evaluated_evaluations, 1)
        self.assertEqual(score.undetermined_evaluations, 0)

    def test_drift_completes_its_coordinate_but_is_excluded_from_the_score(self) -> None:
        score = calculate_readiness_score(
            results=(
                result(),
                result(
                    perspective=EvaluationPerspective.DRIFT, status=EvaluationStatus.FAIL, score=0
                ),
            ),
            planned_evaluations=(
                planned(),
                planned(perspective=EvaluationPerspective.DRIFT),
            ),
        )

        assert score is not None
        self.assertEqual(score.score, 100)
        self.assertEqual(score.evaluated_evaluations, 1)

    def test_rejects_a_duplicated_or_empty_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            calculate_readiness_score(results=(), planned_evaluations=(planned(), planned()))
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            calculate_readiness_score(results=(), planned_evaluations=())

    def test_rejects_a_count_where_the_planned_set_is_required(self) -> None:
        with self.assertRaises(TypeError):
            calculate_readiness_score(results=(result(),), planned_evaluations=1)  # type: ignore[arg-type]


class SegmentReadinessTest(unittest.TestCase):
    """사내 정책과 ISMS-P를 한 Profile로 평가해도 두 준비도는 합치지 않는다.

    합친 하나의 숫자는 어느 기준에 대한 답도 아니다. 사내 기준은 통과하는데 인증 기준은 미달인
    상태가 평균 뒤로 사라지면, 그 보고서를 읽고 할 수 있는 판단이 없다.
    """

    INTERNAL = "INTERNAL_POLICY"
    ISMS = "ISMS_P"

    def test_each_origin_is_scored_over_only_its_own_coordinates(self) -> None:
        scores = calculate_segment_readiness(
            results=(
                result(rule_id="CUST-1", score=100),
                result(
                    rule_id="ISMS-1",
                    resource_id="bucket-002",
                    status=EvaluationStatus.FAIL,
                    score=0,
                ),
            ),
            planned_evaluations=(
                planned(rule_id="CUST-1"),
                planned(rule_id="ISMS-1", resource_id="bucket-002"),
            ),
            rule_kinds={"CUST-1": (self.INTERNAL,), "ISMS-1": (self.ISMS,)},
        )

        by_kind = {entry.kind: entry.score for entry in scores}
        self.assertEqual(sorted(by_kind), [self.INTERNAL, self.ISMS])
        assert by_kind[self.INTERNAL] is not None
        assert by_kind[self.ISMS] is not None
        self.assertEqual(by_kind[self.INTERNAL].score, 100)
        self.assertEqual(by_kind[self.ISMS].score, 0)

    def test_a_rule_serving_both_standards_counts_toward_both(self) -> None:
        """기준선 Rule 대부분이 사내 체크리스트와 ISMS-P 조항을 함께 인용한다."""
        scores = calculate_segment_readiness(
            results=(result(rule_id="SHARED-1", status=EvaluationStatus.FAIL, score=0),),
            planned_evaluations=(planned(rule_id="SHARED-1"),),
            rule_kinds={"SHARED-1": (self.INTERNAL, self.ISMS)},
        )

        self.assertEqual([entry.kind for entry in scores], [self.INTERNAL, self.ISMS])
        for entry in scores:
            assert entry.score is not None
            self.assertEqual(entry.score.score, 0)
            self.assertEqual(entry.score.evaluated_evaluations, 1)

    def test_one_origin_can_be_scored_while_the_other_is_still_running(self) -> None:
        """전체 점수 하나였을 때는 할 수 없던 구분이다 — 미완결 쪽만 `None`이다."""
        scores = calculate_segment_readiness(
            results=(result(rule_id="CUST-1"),),
            planned_evaluations=(
                planned(rule_id="CUST-1"),
                planned(rule_id="ISMS-1", resource_id="bucket-002"),
            ),
            rule_kinds={"CUST-1": (self.INTERNAL,), "ISMS-1": (self.ISMS,)},
        )

        by_kind = {entry.kind: entry.score for entry in scores}
        assert by_kind[self.INTERNAL] is not None
        self.assertEqual(by_kind[self.INTERNAL].score, 100)
        self.assertIsNone(by_kind[self.ISMS])

    def test_a_profile_without_recorded_origins_is_not_split(self) -> None:
        """원본 구분 없이 게시된 Profile은 나눌 근거가 없다. 지금처럼 전체 점수 하나만 쓴다."""
        self.assertEqual(
            calculate_segment_readiness(
                results=(result(),), planned_evaluations=(planned(),), rule_kinds={}
            ),
            (),
        )

    def test_an_origin_whose_rules_never_applied_reports_no_score(self) -> None:
        """Rule은 Profile에 있으나 어떤 Resource에도 적용되지 않았다. 0점이 아니라 점수 없음이다."""
        scores = calculate_segment_readiness(
            results=(result(rule_id="CUST-1"),),
            planned_evaluations=(planned(rule_id="CUST-1"),),
            rule_kinds={"CUST-1": (self.INTERNAL,), "ISMS-1": (self.ISMS,)},
        )

        self.assertIsNone({entry.kind: entry.score for entry in scores}[self.ISMS])


if __name__ == "__main__":
    unittest.main()
