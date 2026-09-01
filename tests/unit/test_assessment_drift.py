"""Unit tests for the deterministic DRIFT perspective derivation."""

import unittest

from apps.backend.assessment import DriftDerivationError, derive_drift_results
from packages.contracts import (
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ScoringMode,
)


def result(
    *,
    perspective: EvaluationPerspective,
    status: EvaluationStatus,
    score: float = 100,
    rule_id: str = "S3-001",
    severity: str = "HIGH",
    evidence: tuple[str, ...] = ("terraform:public-access-block",),
    model_profile_id: str = "assessment-nova-lite-m0-v1",
    rubric_version: str = "mvp-v1",
) -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id=rule_id,
        perspective=perspective,
        status=status,
        severity=severity,
        score=score,
        rationale="fixture",
        evidence_references=evidence,
        rule_version="v1",
        rubric_version=rubric_version,
        model_profile_id=model_profile_id,
    )


def iac(status: EvaluationStatus, **kwargs: object) -> EvaluationResult:
    return result(perspective=EvaluationPerspective.IAC, status=status, **kwargs)  # type: ignore[arg-type]


def actual(status: EvaluationStatus, **kwargs: object) -> EvaluationResult:
    return result(perspective=EvaluationPerspective.AWS_ACTUAL, status=status, **kwargs)  # type: ignore[arg-type]


class DriftDerivationTest(unittest.TestCase):
    def test_agreeing_compliant_perspectives_report_no_drift(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.PASS),),
            actual_results=(actual(EvaluationStatus.PASS),),
        )

        self.assertIs(drift.perspective, EvaluationPerspective.DRIFT)
        self.assertIs(drift.status, EvaluationStatus.PASS)
        self.assertEqual(drift.score, 100)
        self.assertIs(drift.scoring_mode, ScoringMode.CONTINUOUS)

    def test_agreeing_non_compliant_perspectives_report_no_drift(self) -> None:
        """Both sides unsafe is a compliance problem, not a drift problem."""
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.FAIL, score=20),),
            actual_results=(actual(EvaluationStatus.FAIL, score=20),),
        )

        self.assertIs(drift.status, EvaluationStatus.PASS)

    def test_safe_iac_with_unsafe_actual_is_drift(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.PASS),),
            actual_results=(actual(EvaluationStatus.FAIL, score=20),),
        )

        self.assertIs(drift.status, EvaluationStatus.FAIL)
        self.assertEqual(drift.score, 0)
        self.assertIn("AWS Actual state does not", drift.rationale)

    def test_unsafe_iac_with_safe_actual_is_drift(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.FAIL, score=20),),
            actual_results=(actual(EvaluationStatus.PASS),),
        )

        self.assertIs(drift.status, EvaluationStatus.FAIL)
        self.assertIn("approved IaC does not", drift.rationale)

    def test_drift_cites_evidence_from_both_perspectives_without_duplicates(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.PASS, evidence=("terraform:pab", "isms-p#5.2.1")),),
            actual_results=(
                actual(EvaluationStatus.FAIL, score=20, evidence=("aws:s3:pab", "isms-p#5.2.1")),
            ),
        )

        self.assertEqual(drift.evidence_references, ("terraform:pab", "isms-p#5.2.1", "aws:s3:pab"))

    def test_indecisive_perspective_requires_manual_review(self) -> None:
        for status in (EvaluationStatus.MANUAL_REVIEW, EvaluationStatus.INSUFFICIENT_EVIDENCE):
            with self.subTest(status=status):
                (drift,) = derive_drift_results(
                    iac_results=(iac(EvaluationStatus.PASS),),
                    actual_results=(actual(status, score=0),),
                )

                self.assertIs(drift.status, EvaluationStatus.MANUAL_REVIEW)

    def test_missing_counterpart_requires_manual_review(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.PASS),), actual_results=()
        )

        self.assertIs(drift.status, EvaluationStatus.MANUAL_REVIEW)
        self.assertIn("AWS_ACTUAL", drift.rationale)

    def test_execution_error_propagates_so_coverage_stays_incomplete(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.EXECUTION_ERROR, score=0),),
            actual_results=(actual(EvaluationStatus.PASS),),
        )

        self.assertIs(drift.status, EvaluationStatus.EXECUTION_ERROR)

    def test_out_of_scope_on_both_sides_stays_out_of_scope(self) -> None:
        (drift,) = derive_drift_results(
            iac_results=(iac(EvaluationStatus.OUT_OF_SCOPE),),
            actual_results=(actual(EvaluationStatus.OUT_OF_SCOPE),),
        )

        self.assertIs(drift.status, EvaluationStatus.OUT_OF_SCOPE)

    def test_keeps_iac_rule_order_and_appends_actual_only_rules(self) -> None:
        drift = derive_drift_results(
            iac_results=(
                iac(EvaluationStatus.PASS, rule_id="S3-002"),
                iac(EvaluationStatus.PASS, rule_id="S3-001"),
            ),
            actual_results=(
                actual(EvaluationStatus.PASS, rule_id="S3-001"),
                actual(EvaluationStatus.PASS, rule_id="S3-003"),
            ),
        )

        self.assertEqual(tuple(item.rule_id for item in drift), ("S3-002", "S3-001", "S3-003"))

    def test_rejects_results_from_the_wrong_perspective(self) -> None:
        with self.assertRaisesRegex(DriftDerivationError, "IAC"):
            derive_drift_results(
                iac_results=(actual(EvaluationStatus.PASS),),
                actual_results=(actual(EvaluationStatus.PASS),),
            )

    def test_rejects_duplicate_resource_rule_results(self) -> None:
        with self.assertRaisesRegex(DriftDerivationError, "duplicate"):
            derive_drift_results(
                iac_results=(iac(EvaluationStatus.PASS), iac(EvaluationStatus.FAIL, score=20)),
                actual_results=(actual(EvaluationStatus.PASS),),
            )

    def test_rejects_comparison_across_different_model_profiles(self) -> None:
        with self.assertRaisesRegex(DriftDerivationError, "model profiles"):
            derive_drift_results(
                iac_results=(iac(EvaluationStatus.PASS),),
                actual_results=(actual(EvaluationStatus.PASS, model_profile_id="other-profile"),),
            )

    def test_rejects_comparison_across_different_rubrics(self) -> None:
        with self.assertRaisesRegex(DriftDerivationError, "rubrics"):
            derive_drift_results(
                iac_results=(iac(EvaluationStatus.PASS),),
                actual_results=(actual(EvaluationStatus.PASS, rubric_version="mvp-v2"),),
            )

    def test_rejects_severity_disagreement_for_the_same_rule(self) -> None:
        with self.assertRaisesRegex(DriftDerivationError, "severities"):
            derive_drift_results(
                iac_results=(iac(EvaluationStatus.PASS),),
                actual_results=(actual(EvaluationStatus.PASS, severity="LOW"),),
            )

    def test_rejects_non_tuple_results(self) -> None:
        with self.assertRaises(TypeError):
            derive_drift_results(
                iac_results=[iac(EvaluationStatus.PASS)],  # type: ignore[arg-type]
                actual_results=(),
            )
