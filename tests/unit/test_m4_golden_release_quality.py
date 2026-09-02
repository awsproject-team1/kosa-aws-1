import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from apps.backend.assessment.release_quality import (
    GoldenReleaseQualityError,
    evaluate_golden_release_quality,
    load_approved_model_profile,
    load_golden_observation_bundle,
    load_release_golden_cases,
    render_golden_release_markdown,
)
from apps.backend.policy.demo import load_demo_policy_coverage

ROOT = Path(__file__).parents[2]
MANIFEST_PATH = ROOT / "fixtures" / "m4" / "demo_policy_coverage.json"
GOLDEN_PATH = ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json"
PROFILE_PATH = ROOT / "fixtures" / "m1" / "assessment_model_profile.json"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class M4GoldenReleaseQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_demo_policy_coverage(MANIFEST_PATH)
        cls.profile = load_approved_model_profile(PROFILE_PATH)
        cls.cases = load_release_golden_cases(GOLDEN_PATH, manifest=cls.manifest)

    @classmethod
    def _bundle_data(cls) -> dict[str, object]:
        observations: list[dict[str, object]] = []
        resource_by_rule = {case.rule_id: digest(case.rule_id) for case in cls.cases}
        for case in cls.cases:
            expected = case.case
            for run_number in range(1, 6):
                derived = expected.perspective.value == "DRIFT"
                observations.append(
                    {
                        "case_id": expected.case_id,
                        "run_number": run_number,
                        "rule_id": case.rule_id,
                        "rule_version": case.rule_version,
                        "phase": expected.phase.value,
                        "perspective": expected.perspective.value,
                        "status": expected.expected_status.value,
                        "severity": "HIGH",
                        "score": expected.expected_score_min,
                        "evidence_references": list(expected.expected_evidence_references),
                        "model_profile_id": cls.profile.model_profile_id,
                        "rubric_version": cls.profile.rubric_version,
                        "scoring_mode": expected.scoring_mode.value,
                        "resource_id_sha256": resource_by_rule[case.rule_id],
                        "input_artifact_sha256": digest(expected.resource_snapshot_artifact_id),
                        "evaluation_output_sha256": digest(f"{expected.case_id}:{run_number}"),
                        "execution_kind": "CODE_DERIVED" if derived else "BEDROCK",
                        "latency_ms": None if derived else 100 + run_number,
                        "input_tokens": None if derived else 10,
                        "output_tokens": None if derived else 20,
                        "error_code": None,
                    }
                )
        return {
            "schema_version": "m4-golden-observations-v1",
            "execution_id": "m4-release-evaluation-001",
            "generated_at": "2026-09-03T00:00:00+00:00",
            "scenario_id": cls.manifest.scenario_id,
            "runtime_mode": "CUSTOMER_SANDBOX",
            "platform_commit_sha": "a" * 40,
            "repository_commit_sha256": "b" * 64,
            "deployment_id_sha256": "c" * 64,
            "artifact_set_sha256": "d" * 64,
            "model_profile": cls.profile.to_dict(),
            "observations": observations,
        }

    @staticmethod
    def _load(data: dict[str, object]):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return load_golden_observation_bundle(path)

    @classmethod
    def _evaluate(cls, data: dict[str, object]):
        return evaluate_golden_release_quality(
            cls._load(data),
            manifest=cls.manifest,
            cases=cls.cases,
            approved_model_profile=cls.profile,
        )

    def test_complete_customer_sandbox_observations_pass(self) -> None:
        report = self._evaluate(self._bundle_data())

        self.assertTrue(report.passes)
        self.assertEqual(report.case_count, 18)
        self.assertEqual(report.observation_count, 90)
        self.assertEqual(report.bedrock_call_count, 60)
        self.assertEqual(report.code_derived_count, 30)
        self.assertEqual(report.total_input_tokens, 600)
        self.assertEqual(report.total_output_tokens, 1200)
        self.assertEqual(report.bedrock_p95_latency_ms, 105)
        self.assertEqual({item.case_count for item in report.perspective_reports}, {6})

    def test_report_contains_aggregates_not_evidence_or_resource_identifiers(self) -> None:
        report = self._evaluate(self._bundle_data())
        serialized = json.dumps(report.to_dict()) + render_golden_release_markdown(report)

        self.assertNotIn("evidence_references", serialized)
        self.assertNotIn("resource_id_sha256", serialized)
        self.assertNotIn("aws:s3:", serialized)
        self.assertNotIn("terraform:", serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertNotIn("raw_prompt", serialized.lower())

    def test_missing_run_is_rejected_instead_of_lowering_the_denominator(self) -> None:
        data = self._bundle_data()
        data["observations"] = data["observations"][:-1]

        with self.assertRaisesRegex(GoldenReleaseQualityError, "incomplete"):
            self._evaluate(data)

    def test_unapproved_model_profile_is_rejected(self) -> None:
        data = self._bundle_data()
        data["model_profile"]["model_id"] = "another-model"

        with self.assertRaisesRegex(GoldenReleaseQualityError, "approved profile"):
            self._evaluate(data)

    def test_missing_expected_evidence_fails_the_gate(self) -> None:
        data = self._bundle_data()
        observations = data["observations"]
        for observation in observations:
            if observation["rule_id"] == "S3-PUBLIC-001":
                observation["evidence_references"] = []

        report = self._evaluate(data)

        self.assertFalse(report.passes)
        self.assertLess(report.evidence_accuracy, 1)

    def test_score_spread_over_ten_fails_even_when_scores_stay_in_expected_range(self) -> None:
        data = self._bundle_data()
        for observation in data["observations"]:
            if (
                observation["case_id"] == "golden-post-deploy-s3-logging-actual-001"
                and observation["run_number"] == 5
            ):
                observation["score"] = 11

        report = self._evaluate(data)

        self.assertFalse(report.passes)
        self.assertEqual(report.maximum_case_score_spread, 11)

    def test_drift_cannot_claim_a_bedrock_invocation(self) -> None:
        data = self._bundle_data()
        drift = next(
            observation
            for observation in data["observations"]
            if observation["perspective"] == "DRIFT"
        )
        drift["execution_kind"] = "BEDROCK"
        drift["latency_ms"] = 100
        drift["input_tokens"] = 10
        drift["output_tokens"] = 20

        with self.assertRaisesRegex(GoldenReleaseQualityError, "AI/Code boundary"):
            self._evaluate(data)

    def test_drift_must_equal_the_code_derivation_of_the_same_run(self) -> None:
        data = self._bundle_data()
        drift = next(
            observation
            for observation in data["observations"]
            if observation["case_id"] == "golden-post-deploy-s3-public-drift-001"
        )
        drift["status"] = "FAIL"
        drift["score"] = 0

        with self.assertRaisesRegex(GoldenReleaseQualityError, "not derived"):
            self._evaluate(data)

    def test_raw_response_field_is_not_part_of_the_observation_schema(self) -> None:
        data = self._bundle_data()
        changed = deepcopy(data)
        changed["observations"][0]["raw_response"] = "must never be accepted"

        with self.assertRaisesRegex(GoldenReleaseQualityError, "fields must be exactly"):
            self._load(changed)

    def test_fixture_mode_cannot_be_release_evidence(self) -> None:
        data = self._bundle_data()
        data["runtime_mode"] = "FIXTURE"

        with self.assertRaisesRegex(GoldenReleaseQualityError, "CUSTOMER_SANDBOX"):
            self._load(data)


if __name__ == "__main__":
    unittest.main()
