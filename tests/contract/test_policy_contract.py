"""Contract tests for the policy boundary and Golden Dataset handoff."""

import json
import unittest
from pathlib import Path

from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    GoldenDatasetCase,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceKind,
    RuleSeverity,
    ScoringMode,
    SourceReference,
)

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "policy_profile.json"
GOLDEN_CASE_PATH = Path(__file__).parents[2] / "fixtures" / "m0" / "golden_dataset_case.json"


class PolicyContractTest(unittest.TestCase):
    def test_policy_fixture_serializes_the_m0_boundary(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text())
        source = PolicySource(
            source_id=fixture["policy_source"]["source_id"],
            kind=PolicySourceKind(fixture["policy_source"]["kind"]),
            title=fixture["policy_source"]["title"],
            version=fixture["policy_source"]["version"],
            artifact_id=fixture["policy_source"]["artifact_id"],
            content_sha256=fixture["policy_source"]["content_sha256"],
        )
        rule_data = fixture["rule"]
        rule = PolicyRule(
            rule_id=rule_data["rule_id"],
            version=rule_data["version"],
            title=rule_data["title"],
            severity=RuleSeverity(rule_data["severity"]),
            applicable_phases=tuple(
                AssessmentPhase(value) for value in rule_data["applicable_phases"]
            ),
            resource_types=tuple(rule_data["resource_types"]),
            source_references=tuple(
                SourceReference(**reference) for reference in rule_data["source_references"]
            ),
        )
        profile_data = fixture["policy_profile"]
        profile = PolicyProfile(
            policy_profile_id=profile_data["policy_profile_id"],
            version=profile_data["version"],
            rule_references=tuple(
                PolicyRuleReference(**reference) for reference in profile_data["rule_references"]
            ),
        )

        self.assertEqual(source.to_dict(), fixture["policy_source"])
        self.assertEqual(rule.to_dict(), rule_data)
        self.assertEqual(profile.to_dict(), fixture["policy_profile"])

    def test_golden_case_records_versioned_expected_range(self) -> None:
        fixture = json.loads(GOLDEN_CASE_PATH.read_text())
        case = GoldenDatasetCase(
            case_id=fixture["case_id"],
            phase=AssessmentPhase(fixture["phase"]),
            perspective=EvaluationPerspective(fixture["perspective"]),
            rubric_version=fixture["rubric_version"],
            scoring_mode=ScoringMode(fixture["scoring_mode"]),
            resource_snapshot_artifact_id=fixture["resource_snapshot_artifact_id"],
            expected_status=EvaluationStatus(fixture["expected_status"]),
            expected_score_min=fixture["expected_score_min"],
            expected_score_max=fixture["expected_score_max"],
            expected_evidence_references=tuple(fixture["expected_evidence_references"]),
        )

        self.assertEqual(case.to_dict(), fixture)

    def test_rule_requires_a_source_and_phase(self) -> None:
        with self.assertRaisesRegex(ValueError, "applicable_phases must not be empty"):
            PolicyRule(
                rule_id="RULE-001",
                version="v1",
                title="Rule",
                severity=RuleSeverity.HIGH,
                applicable_phases=(),
                resource_types=("AWS::S3::Bucket",),
                source_references=(
                    SourceReference(
                        source_id="source-001", locator="section-1", content_sha256="hash-001"
                    ),
                ),
            )

    def test_golden_case_rejects_inverted_score_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected_score_min must not exceed"):
            GoldenDatasetCase(
                case_id="case-001",
                phase=AssessmentPhase.INITIAL,
                perspective=EvaluationPerspective.IAC,
                rubric_version="v1",
                scoring_mode=ScoringMode.CONTINUOUS,
                resource_snapshot_artifact_id="artifact-001",
                expected_status=EvaluationStatus.FAIL,
                expected_score_min=80,
                expected_score_max=20,
                expected_evidence_references=(),
            )


if __name__ == "__main__":
    unittest.main()
