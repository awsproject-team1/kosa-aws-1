"""Contract tests for the policy boundary and Golden Dataset handoff."""

import json
import unittest
from pathlib import Path

from apps.backend.policy.serialization import profile_from_dict
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    GoldenDatasetCase,
    PolicyControl,
    PolicyProfile,
    PolicyProfileSegment,
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


class PolicyProfileSegmentContractTest(unittest.TestCase):
    """Profile이 여러 정책 원본을 담을 때 그 구분이 저장·복원을 견디는가.

    구분이 저장되지 않으면 보고 단계가 준비도를 사내 정책과 ISMS-P로 나눌 근거를 잃고, 두 기준의
    미달이 하나의 숫자 뒤로 사라진다.
    """

    @staticmethod
    def _reference(rule_id: str) -> PolicyRuleReference:
        return PolicyRuleReference(rule_id=rule_id, version="v1")

    def _profile(self) -> PolicyProfile:
        return PolicyProfile(
            policy_profile_id="profile-combined",
            version="v1",
            rule_references=(self._reference("CUST-1"), self._reference("ISMS-1")),
            segments=(
                PolicyProfileSegment(
                    kind=PolicySourceKind.INTERNAL_POLICY,
                    source_id="src-internal",
                    source_version="ver-1",
                    rule_references=(self._reference("CUST-1"),),
                ),
                PolicyProfileSegment(
                    kind=PolicySourceKind.ISMS_P,
                    source_id="isms-p-2023",
                    source_version="2023-10-31",
                    rule_references=(self._reference("ISMS-1"),),
                ),
            ),
        )

    def test_segments_survive_a_store_and_restore(self) -> None:
        profile = self._profile()

        restored = profile_from_dict(
            {"entity_type": "POLICY_PROFILE", "customer_id": "cust-a", **profile.to_dict()}
        )

        self.assertEqual(restored, profile)
        self.assertEqual(
            restored.rule_kinds(),
            {
                "CUST-1": (PolicySourceKind.INTERNAL_POLICY,),
                "ISMS-1": (PolicySourceKind.ISMS_P,),
            },
        )

    def test_a_profile_stored_before_segments_existed_still_restores(self) -> None:
        """이 계약 이전에 게시된 Profile에는 `segments` 키가 없다."""
        stored = self._profile().to_dict()
        del stored["segments"]

        restored = profile_from_dict(stored)

        self.assertEqual(restored.segments, ())
        self.assertEqual(restored.rule_kinds(), {})

    def test_a_profile_without_segments_omits_the_key_entirely(self) -> None:
        """빈 목록을 쓰면 "구분 안 함"과 "구분했는데 비어 있음"이 저장된 item에서 같아진다."""
        plain = PolicyProfile(
            policy_profile_id="profile-internal",
            version="v1",
            rule_references=(self._reference("CUST-1"),),
        )

        self.assertNotIn("segments", plain.to_dict())

    def test_a_rule_left_out_of_every_segment_is_refused(self) -> None:
        """절반만 분류된 Profile은 나머지를 어느 점수에 넣을지 답할 수 없다."""
        with self.assertRaises(ValueError):
            PolicyProfile(
                policy_profile_id="profile-combined",
                version="v1",
                rule_references=(self._reference("CUST-1"), self._reference("ISMS-1")),
                segments=(
                    PolicyProfileSegment(
                        kind=PolicySourceKind.INTERNAL_POLICY,
                        source_id="src-internal",
                        source_version="ver-1",
                        rule_references=(self._reference("CUST-1"),),
                    ),
                ),
            )

    def test_a_segment_referencing_a_rule_outside_the_profile_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            PolicyProfile(
                policy_profile_id="profile-combined",
                version="v1",
                rule_references=(self._reference("CUST-1"),),
                segments=(
                    PolicyProfileSegment(
                        kind=PolicySourceKind.ISMS_P,
                        source_id="isms-p-2023",
                        source_version="2023-10-31",
                        rule_references=(self._reference("CUST-1"), self._reference("ISMS-1")),
                    ),
                ),
            )


if __name__ == "__main__":
    unittest.main()
