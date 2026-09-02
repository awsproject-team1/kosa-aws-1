import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from apps.backend.policy import load_rule_registry
from apps.backend.policy.demo import (
    DemoPolicyCoverageError,
    load_demo_policy_coverage,
    validate_demo_policy_coverage,
)

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "fixtures" / "m4" / "demo_policy_coverage.json"
INITIAL = ROOT / "fixtures" / "m1" / "golden_dataset_cases.json"
VERIFICATION = ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json"
RULES = ROOT / "fixtures" / "rules"


class DemoPolicyCoverageTest(unittest.TestCase):
    @staticmethod
    def _validate(manifest_path: Path = MANIFEST, *, initial_path: Path = INITIAL):
        return validate_demo_policy_coverage(
            load_demo_policy_coverage(manifest_path),
            registry=load_rule_registry(RULES),
            initial_cases_path=initial_path,
            verification_cases_path=VERIFICATION,
        )

    @staticmethod
    def _temporary_json(value: object) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory()
        Path(directory.name, "value.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )
        return directory

    def test_committed_manifest_covers_profile_policy_and_all_golden_coordinates(self) -> None:
        report = self._validate()

        self.assertEqual(report.profile_rule_count, 6)
        self.assertEqual(report.control_count, 5)
        self.assertEqual(report.policy_evidence_count, 12)
        self.assertEqual(report.golden_case_count, 36)

    def test_manifest_contains_identifiers_and_locators_not_external_demo_data(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        serialized = json.dumps(data).lower()

        for forbidden in (
            "account_id",
            "repository_id",
            "repository_url",
            "role_arn",
            "credential",
            "terraform_body",
            "policy_text",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_profile_version_mismatch_fails_closed(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data["policy_profile_version"] = "replaced-version"
        with self._temporary_json(data) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "profile id/version"):
                self._validate(Path(directory) / "value.json")

    def test_manifest_must_exactly_match_the_profile_rule_allow_list(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data["rules"] = data["rules"][:-1]
        with self._temporary_json(data) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "exactly match"):
                self._validate(Path(directory) / "value.json")

    def test_control_mapping_drift_fails_closed(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data["rules"][0]["control_ids"] = ["ISMS-P-2.10.3"]
        with self._temporary_json(data) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "control_ids"):
                self._validate(Path(directory) / "value.json")

    def test_policy_evidence_must_include_rule_and_control_locators(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data["rules"][0]["policy_evidence_references"] = data["rules"][0][
            "policy_evidence_references"
        ][:-1]
        with self._temporary_json(data) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "policy evidence"):
                self._validate(Path(directory) / "value.json")

    def test_missing_golden_perspective_fails_closed(self) -> None:
        cases = json.loads(INITIAL.read_text(encoding="utf-8"))
        cases = [case for case in cases if case["case_id"] != "golden-s3-public-drift-001"]
        with self._temporary_json(cases) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "exactly every"):
                self._validate(initial_path=Path(directory) / "value.json")

    def test_case_id_rebinding_fails_closed(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        changed = deepcopy(data)
        changed["rules"][0]["golden_cases"][0]["case_id"] = "another-case"
        with self._temporary_json(changed) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "Golden bindings"):
                self._validate(Path(directory) / "value.json")

    def test_duplicate_demo_toggle_is_rejected_at_load(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        data["rules"][1]["demo_toggle"] = data["rules"][0]["demo_toggle"]
        with self._temporary_json(data) as directory:
            with self.assertRaisesRegex(DemoPolicyCoverageError, "duplicate demo_toggle"):
                load_demo_policy_coverage(Path(directory) / "value.json")


if __name__ == "__main__":
    unittest.main()
