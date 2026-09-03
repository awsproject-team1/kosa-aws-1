"""Bedrock adapter keeps model output within the approved assessment boundary."""

import json
import unittest

from apps.backend.assessment import BedrockEvaluationError, BedrockStructuredEvaluator
from apps.backend.policy import PolicyContext
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-s3-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m1-s3-v1",
)
RULE = PolicyRule(
    rule_id="S3-PUBLIC-001",
    version="2026-08-01",
    title="S3 buckets must block public access",
    severity=RuleSeverity.HIGH,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=(
        SourceReference(
            source_id="isms-p",
            source_version="2023-10-31",
            locator="control/5.2.1",
            content_sha256="abc",
        ),
    ),
)
CONTEXT = PolicyContext(
    policy_profile_id="profile-mvp-baseline",
    policy_profile_version="v1",
    phase=AssessmentPhase.INITIAL,
    resource_type="AWS::S3::Bucket",
    rules=(RULE,),
)


class Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


def response(body: dict[str, object]) -> dict[str, object]:
    return {"output": {"message": {"content": [{"text": json.dumps(body)}]}}}


class BedrockStructuredEvaluatorTest(unittest.TestCase):
    def evaluator(self, client: Client) -> BedrockStructuredEvaluator:
        return BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001", "public_access_block": False},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        )

    def test_reconstructs_authoritative_fields_and_invokes_approved_model(self) -> None:
        client = Client(
            response(
                {
                    "status": "FAIL",
                    "score": 21,
                    "rationale": "Public access block is disabled.",
                    "evidence_references": [
                        "aws:s3:GetPublicAccessBlock",
                        "isms-p@2023-10-31#control/5.2.1",
                    ],
                }
            )
        )

        result = self.evaluator(client).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )

        self.assertEqual(result.perspective, EvaluationPerspective.AWS_ACTUAL)
        self.assertEqual(result.rule_id, RULE.rule_id)
        self.assertEqual(result.severity, "HIGH")
        self.assertEqual(result.status, EvaluationStatus.FAIL)
        self.assertEqual(
            result.evidence_references,
            ("aws:s3:GetPublicAccessBlock", "isms-p@2023-10-31#control/5.2.1"),
        )
        self.assertEqual(client.calls[0]["modelId"], PROFILE.model_id)
        message = client.calls[0]["messages"]
        self.assertIn('"resource_id":"bucket-public-001"', message[0]["content"][0]["text"])

    def test_rejects_model_evidence_outside_the_snapshot_and_rule(self) -> None:
        client = Client(
            response(
                {
                    "status": "FAIL",
                    "score": 21,
                    "rationale": "Public access block is disabled.",
                    "evidence_references": ["aws:iam:ListUsers"],
                }
            )
        )

        with self.assertRaisesRegex(BedrockEvaluationError, "outside approved evidence"):
            self.evaluator(client).evaluate(
                resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
            )

    def test_rejects_extra_or_unstructured_model_fields(self) -> None:
        client = Client(
            response(
                {
                    "status": "PASS",
                    "score": 100,
                    "rationale": "Safe.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                    "severity": "LOW",
                }
            )
        )

        with self.assertRaisesRegex(BedrockEvaluationError, "fields are invalid"):
            self.evaluator(client).evaluate(
                resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
            )


AUTHORED_RULE = PolicyRule(
    rule_id="S3-PUBLIC-AUTHORED",
    version="2026-09-03",
    title="Object storage blocks public access",
    severity=RuleSeverity.CRITICAL,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=RULE.source_references,
    control_key="S3_BLOCK_PUBLIC_ACCESS",
    control_catalog_version="governance-control-catalog/2026-09-03",
    evaluation_type=RuleEvaluationType.AWS,
    applicability_semantics="Every bucket the workload writes to.",
    required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
    evaluation_rubric="FAIL unless all four public access block flags are true.",
    severity_guidance="Public read of customer media is a data exposure.",
)


class ApprovedRuleSemanticsTest(unittest.TestCase):
    """승인된 Rule의 실행 의미가 모델에 도달해야 정책 → Rule → Assessment가 연결된다."""

    def _client(self) -> Client:
        return Client(
            response(
                {
                    "status": "FAIL",
                    "score": 5,
                    "rationale": "Two flags are false.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )

    def _evaluate(self, client: Client, rule: PolicyRule) -> None:
        context = PolicyContext(
            policy_profile_id="profile-customer",
            policy_profile_version="v1",
            phase=AssessmentPhase.INITIAL,
            resource_type="AWS::S3::Bucket",
            rules=(rule,),
        )
        BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001"},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        ).evaluate(
            resource_id="bucket-public-001", rule=rule, context=context, model_profile=PROFILE
        )

    def test_the_approved_rubric_and_evidence_capabilities_reach_the_model(self) -> None:
        client = self._client()
        self._evaluate(client, AUTHORED_RULE)
        body = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
        rule_view = body["rule"]
        self.assertEqual(rule_view["evaluation_rubric"], AUTHORED_RULE.evaluation_rubric)
        self.assertEqual(
            rule_view["applicability_semantics"], AUTHORED_RULE.applicability_semantics
        )
        self.assertEqual(rule_view["required_evidence"], ["S3.PUBLIC_ACCESS_BLOCK"])
        self.assertEqual(rule_view["evaluation_type"], "AWS")
        # 판정에 영향을 주지 않는 identity 필드는 prompt에 넣지 않는다.
        self.assertNotIn("control_key", rule_view)
        self.assertNotIn("applicable_phases", rule_view)

    def test_a_legacy_rule_view_carries_no_execution_semantics(self) -> None:
        client = self._client()
        self._evaluate(client, RULE)
        rule_view = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])["rule"]
        self.assertEqual(
            set(rule_view), {"rule_id", "version", "title", "severity", "source_references"}
        )

    def test_the_system_prompt_names_the_rubric_and_the_insufficient_evidence_exit(self) -> None:
        client = self._client()
        self._evaluate(client, AUTHORED_RULE)
        system_text = client.calls[0]["system"][0]["text"]
        self.assertIn("evaluation_rubric", system_text)
        self.assertIn("INSUFFICIENT_EVIDENCE", system_text)


class ModelStatusBoundaryTest(unittest.TestCase):
    def test_the_model_cannot_return_execution_error(self) -> None:
        """EXECUTION_ERROR는 Code가 기록하는 실행 실패이지 모델의 판정이 아니다."""
        client = Client(
            response(
                {
                    "status": "EXECUTION_ERROR",
                    "score": 0,
                    "rationale": "I could not decide.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )
        with self.assertRaisesRegex(BedrockEvaluationError, "reserved for the runtime"):
            BedrockStructuredEvaluator(
                client=client,
                perspective=EvaluationPerspective.AWS_ACTUAL,
                resource_document={"bucket": "bucket-public-001"},
                evidence_references=("aws:s3:GetPublicAccessBlock",),
            ).evaluate(
                resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
            )

    def test_the_model_may_still_report_insufficient_evidence(self) -> None:
        client = Client(
            response(
                {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "score": 0,
                    "rationale": "The document does not show the block configuration.",
                    "evidence_references": ["aws:s3:GetPublicAccessBlock"],
                }
            )
        )
        result = BedrockStructuredEvaluator(
            client=client,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            resource_document={"bucket": "bucket-public-001"},
            evidence_references=("aws:s3:GetPublicAccessBlock",),
        ).evaluate(
            resource_id="bucket-public-001", rule=RULE, context=CONTEXT, model_profile=PROFILE
        )
        self.assertIs(result.status, EvaluationStatus.INSUFFICIENT_EVIDENCE)
