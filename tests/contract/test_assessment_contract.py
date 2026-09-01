"""Contract tests for V3 assessment stages and AI evaluation output."""

import unittest

from packages.contracts import (
    AssessmentComparison,
    AssessmentCoverage,
    AssessmentPhase,
    ComparisonIneligibilityReason,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    Finding,
    FindingResolution,
    FindingResolutionResult,
    ReadinessScore,
    ScoringMode,
)


class AssessmentContractTest(unittest.TestCase):
    def test_coverage_serializes_the_planned_and_completed_evaluation_counts(self) -> None:
        coverage = AssessmentCoverage(planned_evaluations=4, completed_evaluations=3)

        self.assertEqual(
            coverage.to_dict(),
            {"planned_evaluations": 4, "completed_evaluations": 3, "percentage": 75},
        )

    def test_v3_assessment_phase_vocabulary_is_used(self) -> None:
        self.assertEqual(
            [phase.value for phase in AssessmentPhase],
            ["INITIAL", "DEPLOYMENT_READINESS", "POST_DEPLOY_VERIFICATION"],
        )

    def test_evaluation_result_accepts_continuous_score_and_evidence(self) -> None:
        result = EvaluationResult(
            resource_id="s3_bucket_logs",
            rule_id="S3-PUBLIC-001",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL,
            severity="HIGH",
            score=27.5,
            rationale="Public access is allowed.",
            evidence_references=("policy#s3-public", "aws:s3:public-access-block"),
            rule_version="2026-08-01",
            rubric_version="v1",
            model_profile_id="assessment-nova-lite-m0-v1",
        )

        self.assertEqual(result.to_dict()["score"], 27.5)

    def test_evaluation_result_rejects_out_of_range_score(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            EvaluationResult(
                resource_id="resource-001",
                rule_id="RULE-001",
                perspective=EvaluationPerspective.IAC,
                status=EvaluationStatus.PASS,
                severity="LOW",
                score=101,
                rationale="Invalid test value.",
                evidence_references=(),
                rule_version="v1",
                rubric_version="v1",
                model_profile_id="assessment-nova-lite-m0-v1",
            )

    def test_anchored_mode_accepts_only_approved_score_anchors(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved score anchors"):
            EvaluationResult(
                resource_id="resource-001",
                rule_id="RULE-001",
                perspective=EvaluationPerspective.DRIFT,
                status=EvaluationStatus.FAIL,
                severity="HIGH",
                score=25,
                rationale="Anchor-mode test value.",
                evidence_references=(),
                rule_version="v1",
                rubric_version="v1",
                model_profile_id="assessment-nova-lite-m0-v1",
                scoring_mode=ScoringMode.ANCHORED,
            )

    def test_evaluation_result_rejects_an_unknown_perspective(self) -> None:
        with self.assertRaisesRegex(TypeError, "EvaluationPerspective"):
            EvaluationResult(
                resource_id="resource-001",
                rule_id="RULE-001",
                perspective="AWS_ACTUAL",  # type: ignore[arg-type]
                status=EvaluationStatus.FAIL,
                severity="HIGH",
                score=20,
                rationale="Invalid perspective test value.",
                evidence_references=("aws:resource-001",),
                rule_version="v1",
                rubric_version="v1",
                model_profile_id="assessment-nova-lite-m0-v1",
            )

    def test_finding_keeps_the_actionable_evaluation_identity(self) -> None:
        finding = Finding(
            finding_id="finding-001",
            resource_id="s3_bucket_logs",
            rule_id="S3-PUBLIC-001",
            rule_version="2026-08-01",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL,
            severity="HIGH",
            score=27.5,
            rationale="Public access is allowed.",
            evidence_references=("aws:s3:public-access-block",),
        )

        self.assertEqual(finding.to_dict()["finding_id"], "finding-001")

    def test_readiness_score_is_a_bounded_report_projection(self) -> None:
        score = ReadinessScore(score=73.25, evaluated_evaluations=4)

        self.assertEqual(score.to_dict(), {"score": 73.25, "evaluated_evaluations": 4})

    def test_post_deploy_comparison_contract_hides_delta_when_not_comparable(self) -> None:
        comparison = AssessmentComparison(
            source_assessment_id="asm-before",
            verification_assessment_id="asm-after",
            deployment_id="deployment-001",
            comparable=False,
            ineligibility_reasons=(ComparisonIneligibilityReason.MODEL_PROFILE_MISMATCH,),
            source_coverage=AssessmentCoverage(planned_evaluations=1, completed_evaluations=1),
            verification_coverage=AssessmentCoverage(
                planned_evaluations=1, completed_evaluations=1
            ),
            source_readiness_score=ReadinessScore(score=20, evaluated_evaluations=1),
            verification_readiness_score=ReadinessScore(score=100, evaluated_evaluations=1),
            readiness_score_delta=None,
            finding_resolutions=(
                FindingResolutionResult(
                    resource_id="bucket-001",
                    rule_id="S3-001",
                    rule_version="v1",
                    perspective=EvaluationPerspective.AWS_ACTUAL,
                    resolution=FindingResolution.RESOLVED,
                ),
            ),
        )

        self.assertIsNone(comparison.to_dict()["readiness_score_delta"])


if __name__ == "__main__":
    unittest.main()
