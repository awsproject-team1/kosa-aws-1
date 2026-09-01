"""M1 IAC/DRIFT Golden fixtures remain executable contracts."""

import json
import unittest
from pathlib import Path

from apps.backend.assessment import GoldenDatasetRunner
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    GoldenDatasetCase,
    ScoringMode,
)
from packages.contracts.model_profiles import ModelProfile, ModelProfileRole


class M1GoldenCaseTest(unittest.TestCase):
    @staticmethod
    def _cases() -> list[GoldenDatasetCase]:
        root = Path(__file__).parents[2] / "fixtures" / "m1"
        cases = []
        for name in ("golden_dataset_iac_s3_public.json", "golden_dataset_drift_s3_public.json"):
            data = json.loads((root / name).read_text())
            cases.append(
                GoldenDatasetCase(
                    case_id=data["case_id"],
                    phase=AssessmentPhase(data["phase"]),
                    perspective=EvaluationPerspective(data["perspective"]),
                    rubric_version=data["rubric_version"],
                    scoring_mode=ScoringMode(data["scoring_mode"]),
                    resource_snapshot_artifact_id=data["resource_snapshot_artifact_id"],
                    expected_status=EvaluationStatus(data["expected_status"]),
                    expected_score_min=data["expected_score_min"],
                    expected_score_max=data["expected_score_max"],
                    expected_evidence_references=tuple(data["expected_evidence_references"]),
                )
            )
        return cases

    def test_iac_and_drift_cases_are_versioned_and_distinct(self) -> None:
        cases = self._cases()
        self.assertEqual(
            {case.perspective for case in cases},
            {EvaluationPerspective.IAC, EvaluationPerspective.DRIFT},
        )
        self.assertEqual({case.rubric_version for case in cases}, {"m1-three-perspective-v1"})

    def test_m1_profile_pins_the_same_rebaselined_rubric_and_golden_version(self) -> None:
        root = Path(__file__).parents[2] / "fixtures" / "m1"
        data = json.loads((root / "assessment_model_profile.json").read_text())
        profile = ModelProfile(
            model_profile_id=data["model_profile_id"],
            role=ModelProfileRole(data["role"]),
            region=data["region"],
            model_id=data["model_id"],
            prompt_version=data["prompt_version"],
            rubric_version=data["rubric_version"],
            golden_dataset_version=data["golden_dataset_version"],
        )
        self.assertEqual(profile.rubric_version, "m1-three-perspective-v1")
        self.assertEqual(profile.golden_dataset_version, "m1-s3-six-rule-three-perspective-v1")

    def test_iac_and_drift_fixtures_pass_the_repeated_quality_gate(self) -> None:
        class FixtureEvaluator:
            def evaluate_case(self, case: GoldenDatasetCase) -> EvaluationResult:
                return EvaluationResult(
                    resource_id="bucket-public-001",
                    rule_id="S3-PUBLIC-001",
                    perspective=case.perspective,
                    status=case.expected_status,
                    severity="CRITICAL",
                    score=case.expected_score_min,
                    rationale="Golden fixture evaluator.",
                    evidence_references=case.expected_evidence_references,
                    rule_version="2026-08-31",
                    rubric_version=case.rubric_version,
                    model_profile_id="assessment-nova-lite-m1-v2",
                    scoring_mode=case.scoring_mode,
                )

        for case in self._cases():
            self.assertTrue(GoldenDatasetRunner(FixtureEvaluator()).evaluate(case).passes_m0_gate)

    def test_six_m1_rules_have_all_three_perspective_cases(self) -> None:
        root = Path(__file__).parents[2] / "fixtures" / "m1"
        cases = json.loads((root / "golden_dataset_cases.json").read_text())
        self.assertIsInstance(cases, list)
        assert isinstance(cases, list)
        triples = {(case["rule_id"], case["perspective"]) for case in cases}
        rules = {rule_id for rule_id, _ in triples}
        self.assertEqual(len(rules), 6)
        self.assertEqual(
            triples,
            {
                (rule_id, perspective)
                for rule_id in rules
                for perspective in ("IAC", "AWS_ACTUAL", "DRIFT")
            },
        )
        self.assertTrue(all(case["rubric_version"] == "m1-three-perspective-v1" for case in cases))
