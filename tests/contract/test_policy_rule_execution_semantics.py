"""Contract tests for the additive `PolicyRule` execution semantics.

이 파일이 지키는 경계는 하나다: **실행 의미가 있는 Rule과 legacy Rule은 서로 섞이지 않는다.**
legacy fixture Rule은 신규 필드를 갖지 않고, authoring이 만든 Rule은 자기 유형이 요구하는
필드를 전부 갖는다. 절반만 채워진 Rule은 Runtime이 어느 실행 경로로 보내야 할지 알 수 없다.
"""

import unittest
from pathlib import Path

from apps.backend.policy.registry import load_rule_registry
from apps.backend.policy.serialization import rule_from_dict
from packages.contracts import (
    AssessmentPhase,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)
from packages.contracts.policy import (
    MAX_EVALUATION_RUBRIC_LENGTH,
    MAX_EVIDENCE_CAPABILITIES,
)

RULES_PATH = Path(__file__).parents[2] / "fixtures" / "rules"

REFERENCE = SourceReference(
    source_id="internal-policy",
    source_version="2026.1",
    locator="section/3.1",
    content_sha256="a" * 64,
)


def _rule(**overrides: object) -> PolicyRule:
    fields: dict[str, object] = {
        "rule_id": "CUST-S3_BLOCK_PUBLIC_ACCESS-0123456789ab",
        "version": "2026.1",
        "title": "S3 buckets block public access",
        "severity": RuleSeverity.HIGH,
        "applicable_phases": (AssessmentPhase.INITIAL,),
        "resource_types": ("AWS::S3::Bucket",),
        "source_references": (REFERENCE,),
    }
    fields.update(overrides)
    return PolicyRule(**fields)  # type: ignore[arg-type]


class LegacyRuleCompatibilityTest(unittest.TestCase):
    def test_every_committed_fixture_rule_still_loads_as_legacy(self) -> None:
        registry = load_rule_registry(RULES_PATH)

        self.assertEqual(len(registry.rules), 16)
        for rule in registry.rules:
            with self.subTest(rule=rule.rule_id):
                self.assertTrue(rule.is_legacy)
                self.assertIsNone(rule.evaluation_type)
                self.assertEqual(rule.required_evidence, ())
                self.assertEqual(rule.optional_evidence, ())

    def test_a_legacy_rule_serializes_exactly_as_it_did_before_the_new_fields(self) -> None:
        """비어 있는 신규 필드는 `null`로도 나가지 않는다.

        이미 저장된 DynamoDB item과 재직렬화 결과를 그대로 비교하는 멱등 write 경로가 이
        동등성에 걸려 있다. 빈 필드를 내보내기 시작하면 같은 Rule이 "다른 내용"으로 보인다.
        """
        payload = _rule().to_dict()

        self.assertEqual(
            sorted(payload),
            [
                "applicable_phases",
                "resource_types",
                "rule_id",
                "severity",
                "source_references",
                "title",
                "version",
            ],
        )

    def test_a_legacy_rule_must_not_carry_partial_execution_semantics(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not carry execution semantics"):
            _rule(control_key="S3_BLOCK_PUBLIC_ACCESS")
        with self.assertRaisesRegex(ValueError, "must not carry execution semantics"):
            _rule(required_evidence=("S3.PUBLIC_ACCESS_BLOCK",))
        with self.assertRaisesRegex(ValueError, "must not carry execution semantics"):
            _rule(evaluation_rubric="Fail when public access is not blocked.")


class AutomatedRuleInvariantTest(unittest.TestCase):
    def _automated(self, **overrides: object) -> PolicyRule:
        fields: dict[str, object] = {
            "control_key": "S3_BLOCK_PUBLIC_ACCESS",
            "control_catalog_version": "governance-control-catalog/2026-09-03",
            "evaluation_type": RuleEvaluationType.AWS,
            "evaluation_rubric": "Fail when any block-public-access flag is false.",
            "required_evidence": ("S3.PUBLIC_ACCESS_BLOCK",),
        }
        fields.update(overrides)
        return _rule(**fields)

    def test_an_automated_rule_round_trips_every_execution_semantics_field(self) -> None:
        rule = self._automated(
            applicability_semantics="Applies to every bucket in the account.",
            optional_evidence=("S3.BUCKET_POLICY",),
            severity_guidance="Raise to CRITICAL when the bucket holds personal data.",
            exception_semantics="A documented public-hosting exception may apply.",
            compensating_control_semantics="A CloudFront OAC in front of the bucket compensates.",
        )

        restored = rule_from_dict(rule.to_dict())

        self.assertEqual(restored, rule)
        self.assertFalse(restored.is_legacy)

    def test_an_automated_rule_requires_control_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "must carry control_key"):
            self._automated(control_key=None)
        with self.assertRaisesRegex(ValueError, "must carry control_catalog_version"):
            self._automated(control_catalog_version=None)

    def test_an_automated_rule_requires_a_rubric_and_required_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "must carry an evaluation_rubric"):
            self._automated(evaluation_rubric=None)
        with self.assertRaisesRegex(ValueError, "at least one required evidence"):
            self._automated(required_evidence=())

    def test_one_capability_must_not_be_both_required_and_optional(self) -> None:
        """같은 capability가 양쪽에 있으면 pre-flight 판정이 자기 자신과 모순된다."""
        with self.assertRaisesRegex(ValueError, "both required and optional"):
            self._automated(
                required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
                optional_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
            )

    def test_evidence_tuples_reject_duplicates_blanks_and_overflow(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not repeat"):
            self._automated(required_evidence=("S3.PUBLIC_ACCESS_BLOCK", "S3.PUBLIC_ACCESS_BLOCK"))
        with self.assertRaisesRegex(ValueError, "required_evidence item"):
            self._automated(required_evidence=("  ",))
        with self.assertRaisesRegex(ValueError, "at most"):
            self._automated(
                required_evidence=tuple(
                    f"S3.CAP_{index}" for index in range(MAX_EVIDENCE_CAPABILITIES + 1)
                )
            )

    def test_free_text_fields_are_length_capped(self) -> None:
        with self.assertRaisesRegex(ValueError, "evaluation_rubric must be at most"):
            self._automated(evaluation_rubric="x" * (MAX_EVALUATION_RUBRIC_LENGTH + 1))


class ManualRuleInvariantTest(unittest.TestCase):
    def _manual(self, **overrides: object) -> PolicyRule:
        fields: dict[str, object] = {
            "control_key": "ORGANIZATIONAL_CONTROL_MANUAL_REVIEW",
            "control_catalog_version": "governance-control-catalog/2026-09-03",
            "evaluation_type": RuleEvaluationType.MANUAL,
        }
        fields.update(overrides)
        return _rule(**fields)

    def test_a_manual_rule_needs_no_rubric_or_evidence(self) -> None:
        rule = self._manual()

        self.assertEqual(rule.evaluation_type, RuleEvaluationType.MANUAL)
        self.assertEqual(rule_from_dict(rule.to_dict()), rule)

    def test_a_manual_rule_must_not_carry_evidence_capabilities(self) -> None:
        """MANUAL Rule에 evidence를 붙이면 자동 평가가 가능한 것처럼 보인다.

        Runtime은 MANUAL Rule에 Tool도 LLM도 호출하지 않으므로 그 evidence는 영원히 수집되지
        않는다. 수집되지 않을 근거를 요구 항목으로 남기면 `INSUFFICIENT_EVIDENCE`와 구별되지 않는다.
        """
        with self.assertRaisesRegex(ValueError, "MANUAL rule must not carry evidence"):
            self._manual(required_evidence=("S3.PUBLIC_ACCESS_BLOCK",))
        with self.assertRaisesRegex(ValueError, "MANUAL rule must not carry evidence"):
            self._manual(optional_evidence=("S3.PUBLIC_ACCESS_BLOCK",))


class ForbiddenEvaluationOutcomeFieldTest(unittest.TestCase):
    def test_a_rule_carries_no_evaluation_outcome_field(self) -> None:
        """Rule은 평가 **정의**다. 평가 **결과** 필드를 갖는 순간 LLM이 판정을 쓸 자리가 생긴다."""
        payload = _rule().to_dict()

        for forbidden in ("judgment", "score", "source_score", "anchor"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, payload)
                self.assertFalse(hasattr(PolicyRule, forbidden))

    def test_severity_stays_required(self) -> None:
        with self.assertRaises(TypeError):
            PolicyRule(  # type: ignore[call-arg]
                rule_id="CUST-X-0123456789ab",
                version="2026.1",
                title="No severity",
                applicable_phases=(AssessmentPhase.INITIAL,),
                resource_types=("AWS::S3::Bucket",),
                source_references=(REFERENCE,),
            )


if __name__ == "__main__":
    unittest.main()
