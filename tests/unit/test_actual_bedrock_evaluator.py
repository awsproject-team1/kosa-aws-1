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

    def test_complete_evidence_reaches_the_model(self) -> None:
        rule = _authored_rule()
        client = Client()
        result = ActualBedrockEvaluator(
            evidence_loader=_loader(FULL_BLOCK), client=client
        ).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.PASS)
        self.assertEqual(client.calls, 1)

    def test_a_legacy_rule_has_no_gate(self) -> None:
        """legacy Rule은 evidence capability가 없으므로 이전과 같이 모델이 판단한다."""
        rule = _legacy_rule()
        client = Client()
        ActualBedrockEvaluator(evidence_loader=_loader({}), client=client).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertEqual(client.calls, 1)

    def test_an_iac_only_capability_is_not_gated_on_the_aws_read(self) -> None:
        """IaC hint는 authoritative가 아니다. AWS 문서에 없다고 근거 부족으로 읽지 않는다."""
        rule = _authored_rule(
            evaluation_type=RuleEvaluationType.HYBRID,
            required_evidence=("S3.IAC_PUBLIC_ACCESS_BLOCK",),
        )
        client = Client()
        ActualBedrockEvaluator(evidence_loader=_loader({}), client=client).evaluate(
            resource_id="logs-bucket", rule=rule, context=_context(rule), model_profile=PROFILE
        )
        self.assertEqual(client.calls, 1)

    def test_an_unknown_control_fails_closed(self) -> None:
        rule = _authored_rule(control_key="NOT_IN_CATALOG")
        with self.assertRaisesRegex(ActualEvidenceGateError, "does not declare"):
            ActualBedrockEvaluator(evidence_loader=_loader(FULL_BLOCK), client=Client()).evaluate(
                resource_id="logs-bucket",
                rule=rule,
                context=_context(rule),
                model_profile=PROFILE,
            )
