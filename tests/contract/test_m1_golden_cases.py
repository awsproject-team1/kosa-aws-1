"""M1 IAC/DRIFT Golden fixtures remain executable contracts."""

import json
import unittest
from pathlib import Path

from apps.backend.assessment import GoldenDatasetRunner
from apps.backend.policy.context import RESOURCE_EVIDENCE_PREFIXES
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
    def _is_allowed_evidence_reference(reference: str) -> bool:
        if reference.startswith(RESOURCE_EVIDENCE_PREFIXES):
            return True
        source_id, at, source_and_locator = reference.partition("@")
        source_version, hash_mark, locator = source_and_locator.partition("#")
        return bool(source_id and at and source_version and hash_mark and locator)

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

    def test_all_fixture_evidence_uses_an_allowed_resource_or_policy_reference(self) -> None:
        root = Path(__file__).parents[2] / "fixtures" / "m1"
        raw_cases = [
            json.loads((root / name).read_text())
            for name in (
                "golden_dataset_iac_s3_public.json",
                "golden_dataset_drift_s3_public.json",
            )
        ]
        raw_cases.extend(json.loads((root / "golden_dataset_cases.json").read_text()))
        raw_cases.extend(json.loads((root / "golden_dataset_post_deploy_cases.json").read_text()))

        self.assertTrue(
            all(
                self._is_allowed_evidence_reference(reference)
                for raw in raw_cases
                for reference in raw["expected_evidence_references"]
            )
        )

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
        self.assertEqual(
            profile.golden_dataset_version,
            "m3-s3-initial-post-deploy-six-rule-three-perspective-v1",
        )

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

    def test_six_rules_have_all_three_perspective_cases_in_both_assessment_phases(self) -> None:
        root = Path(__file__).parents[2] / "fixtures" / "m1"
        initial_cases = json.loads((root / "golden_dataset_cases.json").read_text())
        verification_cases = json.loads(
            (root / "golden_dataset_post_deploy_cases.json").read_text()
        )
        self.assertIsInstance(initial_cases, list)
        self.assertIsInstance(verification_cases, list)
        assert isinstance(initial_cases, list)
        assert isinstance(verification_cases, list)
        cases = [*initial_cases, *verification_cases]
        coordinates = {(case["phase"], case["rule_id"], case["perspective"]) for case in cases}
        rules = {rule_id for _, rule_id, _ in coordinates}
        self.assertEqual(len(rules), 6)
        self.assertEqual(
            coordinates,
            {
                (phase, rule_id, perspective)
                for phase in ("INITIAL", "POST_DEPLOY_VERIFICATION")
                for rule_id in rules
                for perspective in ("IAC", "AWS_ACTUAL", "DRIFT")
            },
        )
        self.assertTrue(all(case["rubric_version"] == "m1-three-perspective-v1" for case in cases))
        self.assertTrue(
            all(
                case["expected_status"] == "PASS"
                and case["expected_score_min"] == 100
                and case["expected_score_max"] == 100
                for case in verification_cases
            )
        )

    def test_all_six_by_three_by_two_phase_cases_pass_the_repeated_quality_gate(self) -> None:
        root = Path(__file__).parents[2] / "fixtures" / "m1"
        fixture_cases = [
            *json.loads((root / "golden_dataset_cases.json").read_text()),
            *json.loads((root / "golden_dataset_post_deploy_cases.json").read_text()),
        ]

        class FixtureEvaluator:
            def evaluate_case(self, case: GoldenDatasetCase) -> EvaluationResult:
                raw = next(raw for raw in fixture_cases if raw["case_id"] == case.case_id)
                return EvaluationResult(
                    resource_id="bucket-public-001",
                    rule_id=raw["rule_id"],
                    perspective=case.perspective,
                    status=case.expected_status,
                    severity="HIGH",
                    score=case.expected_score_min,
                    rationale="Golden fixture evaluator.",
                    evidence_references=case.expected_evidence_references,
                    rule_version="2026-08-31",
                    rubric_version=case.rubric_version,
                    model_profile_id="assessment-nova-lite-m1-v2",
                    scoring_mode=case.scoring_mode,
                )

        for raw in fixture_cases:
            case = GoldenDatasetCase(
                case_id=raw["case_id"],
                phase=AssessmentPhase(raw["phase"]),
                perspective=EvaluationPerspective(raw["perspective"]),
                rubric_version=raw["rubric_version"],
                scoring_mode=ScoringMode(raw["scoring_mode"]),
                resource_snapshot_artifact_id=raw["resource_snapshot_artifact_id"],
                expected_status=EvaluationStatus(raw["expected_status"]),
                expected_score_min=raw["expected_score_min"],
                expected_score_max=raw["expected_score_max"],
                expected_evidence_references=tuple(raw["expected_evidence_references"]),
            )
            self.assertTrue(GoldenDatasetRunner(FixtureEvaluator()).evaluate(case).passes_m0_gate)
