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
        SourceReference(source_id="isms-p", locator="control/5.2.1", content_sha256="abc"),
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
                    "evidence_references": ["aws:s3:GetPublicAccessBlock", "isms-p#control/5.2.1"],
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
            result.evidence_references, ("aws:s3:GetPublicAccessBlock", "isms-p#control/5.2.1")
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
