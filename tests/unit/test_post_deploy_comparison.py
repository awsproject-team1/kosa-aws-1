"""ADR-0020 deterministic Post-Deploy Verification projections."""

import unittest

from apps.backend.assessment import (
    ComparisonAssessment,
    PlannedEvaluation,
    compare_post_deploy_assessments,
)
from apps.backend.assessment.reporting import AssessmentReport
from packages.contracts import (
    AssessmentCoverage,
    ComparisonIneligibilityReason,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    FindingResolution,
    ReadinessScore,
)


def result(
    status: EvaluationStatus,
    *,
    rule_version: str = "v1",
    rule_id: str = "S3-001",
    perspective: EvaluationPerspective = EvaluationPerspective.AWS_ACTUAL,
) -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id=rule_id,
        perspective=perspective,
        status=status,
        severity="HIGH",
        score=100 if status is EvaluationStatus.PASS else 20,
        rationale="fixture",
        evidence_references=("aws:s3:fixture",),
        rule_version=rule_version,
        rubric_version="m1-v1",
        model_profile_id="assessment-profile-v1",
    )


def assessment(
    assessment_id: str,
    results: tuple[EvaluationResult, ...],
    *,
    score: float | None = 20,
    plan: tuple[PlannedEvaluation, ...] | None = None,
    profile: str = "assessment-profile-v1",
    rubric: str = "m1-v1",
) -> ComparisonAssessment:
    plan = plan or tuple(
        PlannedEvaluation(
            resource_id=item.resource_id, rule_id=item.rule_id, perspective=item.perspective
        )
        for item in results
    )
    report = AssessmentReport(
        assessment_id=assessment_id,
        results=results,
        findings=(),
        coverage=AssessmentCoverage(
            planned_evaluations=len(plan),
            completed_evaluations=len(plan) if score is not None else 0,
        ),
        readiness_score=(
            ReadinessScore(score=score, evaluated_evaluations=len(plan)) if score else None
        ),
    )
    return ComparisonAssessment(
        assessment_id=assessment_id,
        model_profile_id=profile,
        rubric_version=rubric,
        planned_evaluations=plan,
        report=report,
    )


class PostDeployComparisonTest(unittest.TestCase):
    def test_resolved_comparison_has_score_delta(self) -> None:
        comparison = compare_post_deploy_assessments(
            deployment_id="dep-001",
            source=assessment("asm-before", (result(EvaluationStatus.FAIL),), score=20),
            verification=assessment("asm-after", (result(EvaluationStatus.PASS),), score=100),
        )

        self.assertTrue(comparison.comparable)
        self.assertEqual(comparison.readiness_score_delta, 80)
        self.assertEqual(comparison.finding_resolutions[0].resolution, FindingResolution.RESOLVED)

    def test_every_resolution_is_deterministic(self) -> None:
        cases = (
            (EvaluationStatus.FAIL, EvaluationStatus.FAIL, FindingResolution.UNRESOLVED),
            (EvaluationStatus.PASS, EvaluationStatus.FAIL, FindingResolution.REGRESSED),
            (
                EvaluationStatus.FAIL,
                EvaluationStatus.OUT_OF_SCOPE,
                FindingResolution.NO_LONGER_APPLICABLE,
            ),
            (
                EvaluationStatus.FAIL,
                EvaluationStatus.MANUAL_REVIEW,
                FindingResolution.INDETERMINATE,
            ),
        )
        for before, after, expected in cases:
            with self.subTest(before=before, after=after):
                comparison = compare_post_deploy_assessments(
                    deployment_id="dep-001",
                    source=assessment("asm-before", (result(before),)),
                    verification=assessment("asm-after", (result(after),)),
                )
                self.assertEqual(comparison.finding_resolutions[0].resolution, expected)

    def test_rule_version_change_and_absent_counterpart_are_indeterminate(self) -> None:
        changed = compare_post_deploy_assessments(
            deployment_id="dep-001",
            source=assessment("asm-before", (result(EvaluationStatus.FAIL),)),
            verification=assessment(
                "asm-after", (result(EvaluationStatus.PASS, rule_version="v2"),)
            ),
        )
        absent = compare_post_deploy_assessments(
            deployment_id="dep-001",
            source=assessment("asm-before", (result(EvaluationStatus.FAIL),)),
            verification=assessment(
                "asm-after",
                (),
                plan=(
                    PlannedEvaluation(
                        resource_id="bucket-001",
                        rule_id="S3-001",
                        perspective=EvaluationPerspective.AWS_ACTUAL,
                    ),
                ),
            ),
        )
        self.assertEqual(changed.finding_resolutions[0].resolution, FindingResolution.INDETERMINATE)
        self.assertEqual(absent.finding_resolutions[0].resolution, FindingResolution.INDETERMINATE)

    def test_partial_report_is_rejected(self) -> None:
        for field_name in ("next_cursor", "findings_next_cursor"):
            with self.subTest(field_name=field_name):
                complete = assessment("asm-001", (result(EvaluationStatus.FAIL),))
                report = AssessmentReport(
                    assessment_id=complete.report.assessment_id,
                    results=complete.report.results,
                    findings=complete.report.findings,
                    coverage=complete.report.coverage,
                    readiness_score=complete.report.readiness_score,
                    **{field_name: "opaque-next-page"},
                )

                with self.assertRaisesRegex(
                    ValueError, "report must contain complete results and findings"
                ):
                    ComparisonAssessment(
                        assessment_id=complete.assessment_id,
                        model_profile_id=complete.model_profile_id,
                        rubric_version=complete.rubric_version,
                        planned_evaluations=complete.planned_evaluations,
                        report=report,
                    )

    def test_noncomparable_inputs_hide_delta_with_all_reasons(self) -> None:
        source = assessment("asm-before", (result(EvaluationStatus.FAIL),), score=None)
        verification = assessment(
            "asm-after",
            (result(EvaluationStatus.PASS),),
            score=100,
            plan=(
                PlannedEvaluation(
                    resource_id="bucket-002",
                    rule_id="S3-002",
                    perspective=EvaluationPerspective.AWS_ACTUAL,
                ),
            ),
            profile="other-profile",
            rubric="m1-v2",
        )
        comparison = compare_post_deploy_assessments(
            deployment_id="dep-001", source=source, verification=verification
        )

        self.assertFalse(comparison.comparable)
        self.assertIsNone(comparison.readiness_score_delta)
        self.assertEqual(
            comparison.ineligibility_reasons,
            (
                ComparisonIneligibilityReason.SOURCE_READINESS_UNAVAILABLE,
                ComparisonIneligibilityReason.PLANNED_EVALUATIONS_MISMATCH,
                ComparisonIneligibilityReason.MODEL_PROFILE_MISMATCH,
                ComparisonIneligibilityReason.RUBRIC_VERSION_MISMATCH,
            ),
        )


if __name__ == "__main__":
    unittest.main()
