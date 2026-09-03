"""S3 Actual evaluator composes the read-only evidence and Bedrock boundaries."""

import json
import unittest

from agent.runtime import AwsResourceView, MockAwsResourceTool
from apps.backend.assessment import ActualBedrockEvaluator, ActualEvidenceLoader
from apps.backend.policy import PolicyContext
from packages.contracts import (
    AssessmentPhase,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleSeverity,
    SourceReference,
)


class Client:
    def converse(self, **kwargs):
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
