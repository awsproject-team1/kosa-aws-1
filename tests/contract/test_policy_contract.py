"""Contract tests for the policy boundary and Golden Dataset handoff."""

import json
import unittest
from pathlib import Path

from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    GoldenDatasetCase,
    PolicyControl,
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
CONTROLS_PATH = Path(__file__).parents[2] / "fixtures" / "rules" / "controls.json"


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
        self.assertEqual(
            rule.source_references[0].evidence_reference,
            "isms-p-2023@2023.1#control/5.2.1",
        )

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
                        source_id="source-001",
                        source_version="v1",
                        locator="section-1",
                        content_sha256="hash-001",
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

    def test_controls_fixture_serializes_the_control_mapping(self) -> None:
        fixture = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))

        for entry in fixture:
            control = PolicyControl(
                control_id=entry["control_id"],
                title=entry["title"],
                source_reference=SourceReference(**entry["source_reference"]),
                rule_references=tuple(
                    PolicyRuleReference(**reference) for reference in entry["rule_references"]
                ),
            )

            self.assertEqual(control.to_dict(), entry)

    def test_source_reference_pins_a_source_version_and_renders_evidence(self) -> None:
        reference = SourceReference(
            source_id="isms-p-2023",
            source_version="2023-10-31",
            locator="control/2.6.2",
            content_sha256="hash-001",
        )

        self.assertEqual(reference.evidence_reference, "isms-p-2023@2023-10-31#control/2.6.2")
        self.assertEqual(
            reference.to_dict(),
            {
                "source_id": "isms-p-2023",
                "source_version": "2023-10-31",
                "locator": "control/2.6.2",
                "content_sha256": "hash-001",
            },
        )

    def test_source_reference_requires_a_source_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_version must be a non-empty string"):
            SourceReference(
                source_id="isms-p-2023",
                source_version="  ",
                locator="control/2.6.2",
                content_sha256="hash-001",
            )

    def test_control_requires_at_least_one_rule_reference(self) -> None:
        with self.assertRaisesRegex(ValueError, "rule_references must not be empty"):
            PolicyControl(
                control_id="ISMS-P-2.6.2",
                title="정보시스템 접근",
                source_reference=SourceReference(
                    source_id="isms-p-2023",
                    source_version="2023-10-31",
                    locator="control/2.6.2",
                    content_sha256="hash-001",
                ),
                rule_references=(),
            )

    def test_control_rejects_a_non_source_reference(self) -> None:
        with self.assertRaisesRegex(TypeError, "source_reference must be a SourceReference"):
            PolicyControl(
                control_id="ISMS-P-2.6.2",
                title="정보시스템 접근",
                source_reference={"source_id": "isms-p-2023"},
                rule_references=(PolicyRuleReference(rule_id="S3-ACL-001", version="v1"),),
            )


if __name__ == "__main__":
    unittest.main()
