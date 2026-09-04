"""S3 Actual evaluator composes the read-only evidence and Bedrock boundaries."""

import json
import unittest

from agent.runtime import AwsResourceView, MockAwsResourceTool
from apps.backend.assessment import ActualBedrockEvaluator, ActualEvidenceLoader
from apps.backend.assessment.actual_evaluator import ActualEvidenceGateError
from apps.backend.policy import PolicyContext
from apps.backend.policy.control_catalog import CONTROL_CATALOG_VERSION
from packages.contracts import (
    AssessmentPhase,
    DecisionSource,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)


class Client:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "status": "PASS",
                                    "score": 100,
                                    "rationale": "Safe.",
                                    "evidence_references": [
                                        "aws:s3:bucket/logs-bucket#read-resource"
                                    ],
                                }
                            )
                        }
                    ]
                }
            }
        }


class ActualBedrockEvaluatorTest(unittest.TestCase):
    def test_returns_an_aws_actual_result_from_scoped_s3_evidence(self):
        rule = PolicyRule(
            rule_id="S3-001",
            version="v1",
            title="S3",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(
                SourceReference(
                    source_id="p",
                    source_version="v1",
                    locator="p#1",
                    content_sha256="x",
                ),
            ),
        )
        context = PolicyContext(
            policy_profile_id="p",
            policy_profile_version="v1",
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
            rules=(rule,),
        )
        profile = ModelProfile(
            model_profile_id="m",
            role=ModelProfileRole.ASSESSMENT,
            region="us-east-1",
            model_id="amazon.nova-lite-v1:0",
            prompt_version="v1",
            rubric_version="v1",
            golden_dataset_version="v1",
        )
        loader = ActualEvidenceLoader(
            tool=MockAwsResourceTool(
                customer_id="cust",
                aws_account_id="123",
                resources=(
                    AwsResourceView(
                        aws_account_id="123",
                        resource_type="AWS::S3::Bucket",
                        resource_id="logs-bucket",
                        attributes={},
                    ),
                ),
            ),
            customer_id="cust",
            aws_account_id="123",
            resource_type="AWS::S3::Bucket",
        )
        result = ActualBedrockEvaluator(evidence_loader=loader, client=Client()).evaluate(
            resource_id="logs-bucket", rule=rule, context=context, model_profile=profile
        )
        self.assertEqual(result.perspective.value, "AWS_ACTUAL")


SOURCE = SourceReference(source_id="p", source_version="v1", locator="p#1", content_sha256="x")
PROFILE = ModelProfile(
    model_profile_id="m",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="v1",
    rubric_version="v1",
    golden_dataset_version="v1",
)
FULL_BLOCK = {
    "public_access_block": {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
}


def _authored_rule(**overrides: object) -> PolicyRule:
    values: dict[str, object] = {
        "rule_id": "S3-PUBLIC-AUTHORED",
        "version": "2026-09-03",
        "title": "Object storage blocks public access",
        "severity": RuleSeverity.CRITICAL,
        "applicable_phases": (AssessmentPhase.INITIAL,),
        "resource_types": ("AWS::S3::Bucket",),
        "source_references": (SOURCE,),
        "control_key": "S3_BLOCK_PUBLIC_ACCESS",
        "control_catalog_version": CONTROL_CATALOG_VERSION,
        "evaluation_type": RuleEvaluationType.AWS,
        "required_evidence": ("S3.PUBLIC_ACCESS_BLOCK",),
        "evaluation_rubric": "FAIL unless all four flags are true.",
    }
    values.update(overrides)
    return PolicyRule(**values)


def _legacy_rule() -> PolicyRule:
    return PolicyRule(
        rule_id="S3-PUBLIC-001",
        version="2026-08-31",
        title="S3",
        severity=RuleSeverity.CRITICAL,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=("AWS::S3::Bucket",),
        source_references=(SOURCE,),
    )


def _loader(attributes: dict[str, object]) -> ActualEvidenceLoader:
    return ActualEvidenceLoader(
        tool=MockAwsResourceTool(
            customer_id="cust",
            aws_account_id="123",
            resources=(
                AwsResourceView(
                    aws_account_id="123",
                    resource_type="AWS::S3::Bucket",
                    resource_id="logs-bucket",
                    attributes=attributes,
                ),
            ),
        ),
        customer_id="cust",
        aws_account_id="123",
        resource_type="AWS::S3::Bucket",
    )


def _context(rule: PolicyRule) -> PolicyContext:
    return PolicyContext(
        policy_profile_id="p",
        policy_profile_version="v1",
        phase=AssessmentPhase.INITIAL,
        resource_type="AWS::S3::Bucket",
        rules=(rule,),
    )


class EvidencePreflightGateTest(unittest.TestCase):
    """근거가 없는 좌표는 Code가 INSUFFICIENT_EVIDENCE를 만들고 모델을 부르지 않는다."""

    def test_missing_required_evidence_skips_the_model(self) -> None:
        rule = _authored_rule()
        client = Client()
        result = ActualBedrockEvaluator(evidence_loader=_loader({}), client=client).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(client.calls, 0)
        self.assertEqual(result.severity, "CRITICAL")
        self.assertIn("attributes.public_access_block.BlockPublicAcls", result.rationale)
        # 무엇을 읽었는지(aws locator)와 어떤 정책 판본인지(source)가 결과에 남는다.
        self.assertIn("aws:s3:bucket/logs-bucket#read-resource", result.evidence_references)
        self.assertIn("p@v1#p#1", result.evidence_references)
        self.assertEqual(result.rule_version, rule.version)
        self.assertEqual(result.model_profile_id, PROFILE.model_profile_id)

    def test_a_partially_present_block_is_still_insufficient(self) -> None:
        rule = _authored_rule()
        client = Client()
        result = ActualBedrockEvaluator(
            evidence_loader=_loader({"public_access_block": {"BlockPublicAcls": False}}),
            client=client,
        ).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(client.calls, 0)
        self.assertNotIn("BlockPublicAcls", result.rationale)
        self.assertIn("attributes.public_access_block.IgnorePublicAcls", result.rationale)

    def test_a_decidable_capability_is_settled_without_the_model(self) -> None:
        """`S3.PUBLIC_ACCESS_BLOCK`은 술어가 선언돼 있다 — 네 플래그가 모두 참인지는 사실이다.

        사실을 모델에게 물으면 정확도·점수 입도·비용을 한꺼번에 잃는다
        (`apps/backend/assessment/deterministic.py`). 근거는 그대로 남는다.
        """
        rule = _authored_rule()
        client = Client()

        result = ActualBedrockEvaluator(
            evidence_loader=_loader(FULL_BLOCK), client=client
        ).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )

        self.assertIs(result.status, EvaluationStatus.PASS)
        self.assertEqual(result.score, 100.0)
        self.assertEqual(client.calls, 0)
        self.assertIn("aws:s3:bucket/logs-bucket#read-resource", result.evidence_references)

    def test_partial_compliance_fails_and_keeps_the_observation_detail(self) -> None:
        """모델은 이 입력을 PASS로 봤다(라이브 측정). 코드는 FAIL이고, 무엇이 빠졌는지 남긴다.

        score는 status가 정하므로 0이다. 비율 75는 준비도 평균에 넣을 값이 아니라 리포트와
        조치가 읽을 관측 상세다 — 분모가 리소스 개수라 점수로 쓰면 같은 위험이 리소스를 더
        붙일수록 준비도를 올린다.
        """
        rule = _authored_rule()
        client = Client()

        result = ActualBedrockEvaluator(
            evidence_loader=_loader(
                {
                    "public_access_block": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": False,
                    }
                }
            ),
            client=client,
        ).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )

        self.assertIs(result.status, EvaluationStatus.FAIL)
        self.assertEqual(result.score, 0.0)
        self.assertEqual((result.observed_satisfied, result.observed_total), (3, 4))
        self.assertIs(result.decided_by, DecisionSource.CODE)
        self.assertIn("RestrictPublicBuckets", result.rationale)
        self.assertEqual(client.calls, 0)

    def test_a_legacy_rule_the_catalog_maps_passes_the_same_gate(self) -> None:
        """배포된 baseline Profile은 legacy Rule로 돼 있다. 그것이 게이트 밖에 있으면 안 된다."""
        rule = _legacy_rule()
        client = Client()
        result = ActualBedrockEvaluator(evidence_loader=_loader({}), client=client).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIs(result.decided_by, DecisionSource.CODE)
        self.assertEqual(client.calls, 0)

    def test_a_legacy_rule_the_catalog_maps_is_decided_by_code_when_it_can_be(self) -> None:
        rule = _legacy_rule()
        client = Client()
        result = ActualBedrockEvaluator(
            evidence_loader=_loader(FULL_BLOCK), client=client
        ).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.PASS)
        self.assertIs(result.decided_by, DecisionSource.CODE)
        self.assertEqual(client.calls, 0)

    def test_a_legacy_rule_the_catalog_does_not_know_is_left_to_the_model(self) -> None:
        """Catalog가 모르는 Rule에 대해서는 아무 선언도 없으므로 이전과 같이 모델이 판단한다."""
        rule = PolicyRule(
            rule_id="CUSTOM-LEGACY-1",
            version="v1",
            title="Unmapped",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(SOURCE,),
        )
        client = Client()
        result = ActualBedrockEvaluator(evidence_loader=_loader({}), client=client).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertEqual(client.calls, 1)
        self.assertIs(result.decided_by, DecisionSource.MODEL)

    def test_a_capability_without_an_aws_binding_is_insufficient_not_judged(self) -> None:
        """Catalog가 "이 관점의 근거가 없다"고 이미 아는 좌표에서 모델에게 묻지 않는다.

        예전에는 이 경우 검사를 건너뛰고 모델을 불렀다. baseline의 S3 ACL Rule이 그렇게
        public-access-block 플래그를 근거로 인용하며 PASS를 냈다 — 답이 문서에 존재할 수 없는데도.
        """
        rule = _authored_rule(
            evaluation_type=RuleEvaluationType.HYBRID,
            required_evidence=("S3.IAC_PUBLIC_ACCESS_BLOCK",),
        )
        client = Client()
        result = ActualBedrockEvaluator(
            evidence_loader=_loader(FULL_BLOCK), client=client
        ).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(client.calls, 0)
        self.assertIn(
            "declares no AWS_ACTUAL evidence for S3.IAC_PUBLIC_ACCESS_BLOCK", result.rationale
        )

    def test_the_baseline_s3_rules_without_aws_evidence_are_not_judged_by_the_model(self) -> None:
        """ACL·Bucket Policy·TLS·Logging은 S3 read 문서에 답이 없다. 넷 다 모델 호출 0회."""
        for rule_id in ("S3-ACL-001", "S3-POLICY-001", "S3-TLS-001", "S3-LOGGING-001"):
            with self.subTest(rule=rule_id):
                rule = PolicyRule(
                    rule_id=rule_id,
                    version="2026-08-31",
                    title=rule_id,
                    severity=RuleSeverity.HIGH,
                    applicable_phases=(AssessmentPhase.INITIAL,),
                    resource_types=("AWS::S3::Bucket",),
                    source_references=(SOURCE,),
                )
                client = Client()
                result = ActualBedrockEvaluator(
                    evidence_loader=_loader(FULL_BLOCK), client=client
                ).evaluate(
                    resource_id="logs-bucket",
                    rule=rule,
                    context=_context(rule),
                    model_profile=PROFILE,
                )
                self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
                self.assertEqual(client.calls, 0)

    def test_an_unknown_control_fails_closed(self) -> None:
        rule = _authored_rule(control_key="NOT_IN_CATALOG")
        with self.assertRaisesRegex(ActualEvidenceGateError, "does not declare"):
            ActualBedrockEvaluator(evidence_loader=_loader(FULL_BLOCK), client=Client()).evaluate(
                resource_id="logs-bucket",
                rule=rule,
                context=_context(rule),
                model_profile=PROFILE,
            )
