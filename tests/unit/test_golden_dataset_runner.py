"""Tests for repeated Golden Dataset quality checks."""

import unittest

from apps.backend.assessment import GoldenDatasetRunner
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    GoldenDatasetCase,
    ScoringMode,
)

CASE = GoldenDatasetCase(
    case_id="golden-s3-public-001",
    phase=AssessmentPhase.INITIAL,
    perspective=EvaluationPerspective.AWS_ACTUAL,
    rubric_version="mvp-v1",
    scoring_mode=ScoringMode.CONTINUOUS,
    resource_snapshot_artifact_id="art-aws-snapshot-s3-public-001",
    expected_status=EvaluationStatus.FAIL,
    expected_score_min=0,
    expected_score_max=30,
    expected_evidence_references=("aws:s3:public-access-block", "isms-p#5.2.1"),
)


class Evaluator:
    def __init__(self, outcomes: tuple[EvaluationResult, ...]) -> None:
        self.outcomes = outcomes
        self.index = 0

    def evaluate_case(self, case: GoldenDatasetCase) -> EvaluationResult:
        outcome = self.outcomes[self.index % len(self.outcomes)]
        self.index += 1
        return outcome


def result(
    *, score: float, evidence: tuple[str, ...] = CASE.expected_evidence_references
) -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-public-001",
        rule_id="S3-PUBLIC-001",
        perspective=EvaluationPerspective.AWS_ACTUAL,
        status=EvaluationStatus.FAIL,
        severity="HIGH",
        score=score,
        rationale="Public access block is disabled",
        evidence_references=evidence,
        rule_version="2026-08-01",
        rubric_version="mvp-v1",
        model_profile_id="assessment-nova-lite-m0-v1",
    )


class GoldenDatasetRunnerTest(unittest.TestCase):
    def test_passes_repeated_case_within_required_variance(self) -> None:
        report = GoldenDatasetRunner(
            Evaluator((result(score=20), result(score=24), result(score=27)))
        ).evaluate(CASE)

        self.assertTrue(report.passes_m0_gate)
        self.assertEqual(report.score_spread, 7)
        self.assertEqual(report.status_accuracy, 1)

    def test_fails_when_required_evidence_is_missing(self) -> None:
        report = GoldenDatasetRunner(Evaluator((result(score=20, evidence=()),))).evaluate(CASE)

        self.assertFalse(report.passes_m0_gate)
        self.assertEqual(report.evidence_accuracy, 0)

    def test_fails_when_a_score_is_outside_the_case_range(self) -> None:
        report = GoldenDatasetRunner(Evaluator((result(score=31),))).evaluate(CASE)

        self.assertFalse(report.passes_m0_gate)
        self.assertEqual(report.score_accuracy, 0)

    def test_requires_at_least_two_runs(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 2"):
            GoldenDatasetRunner(Evaluator((result(score=20),))).evaluate(CASE, repetitions=1)

    def test_rejects_a_result_for_a_different_rubric(self) -> None:
        mismatched = EvaluationResult(
            resource_id="bucket-public-001",
            rule_id="S3-PUBLIC-001",
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL,
            severity="HIGH",
            score=20,
            rationale="Public access block is disabled",
            evidence_references=CASE.expected_evidence_references,
            rule_version="2026-08-01",
            rubric_version="another-rubric",
            model_profile_id="assessment-nova-lite-m0-v1",
        )
        with self.assertRaisesRegex(ValueError, "rubric version"):
            GoldenDatasetRunner(Evaluator((mismatched,))).evaluate(CASE)
