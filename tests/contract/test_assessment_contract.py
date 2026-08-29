"""Contract tests for V3 assessment stages and AI evaluation output."""

import unittest

from packages.contracts import (
    AssessmentPhase,
    EvaluationResult,
    EvaluationStatus,
    ScoringMode,
)


class AssessmentContractTest(unittest.TestCase):
    def test_v3_assessment_phase_vocabulary_is_used(self) -> None:
        self.assertEqual(
            [phase.value for phase in AssessmentPhase],
            ["INITIAL", "DEPLOYMENT_READINESS", "POST_DEPLOY_VERIFICATION"],
        )

    def test_evaluation_result_accepts_continuous_score_and_evidence(self) -> None:
        result = EvaluationResult(
            resource_id="s3_bucket_logs",
            rule_id="S3-PUBLIC-001",
            status=EvaluationStatus.FAIL,
            severity="HIGH",
            score=27.5,
            rationale="Public access is allowed.",
            evidence_references=("policy#s3-public", "aws:s3:public-access-block"),
            rule_version="2026-08-01",
            rubric_version="v1",
        )

        self.assertEqual(result.to_dict()["score"], 27.5)

    def test_evaluation_result_rejects_out_of_range_score(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            EvaluationResult(
                resource_id="resource-001",
                rule_id="RULE-001",
                status=EvaluationStatus.PASS,
                severity="LOW",
                score=101,
                rationale="Invalid test value.",
                evidence_references=(),
                rule_version="v1",
                rubric_version="v1",
            )

    def test_anchored_mode_accepts_only_approved_score_anchors(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved score anchors"):
            EvaluationResult(
                resource_id="resource-001",
                rule_id="RULE-001",
                status=EvaluationStatus.FAIL,
                severity="HIGH",
                score=25,
                rationale="Anchor-mode test value.",
                evidence_references=(),
                rule_version="v1",
                rubric_version="v1",
                scoring_mode=ScoringMode.ANCHORED,
            )


if __name__ == "__main__":
    unittest.main()
