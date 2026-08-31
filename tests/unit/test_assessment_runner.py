"""Assessment runner rejects model output that escapes its Policy Context."""

import unittest

from apps.backend.assessment import AssessmentRunner, EvaluationContractError
from apps.backend.policy import PolicyContext
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    PolicyRule,
    RuleSeverity,
    SourceReference,
)


def context() -> PolicyContext:
    rule = PolicyRule(
        rule_id="S3-001",
        version="v1",
        title="S3 block public access",
        severity=RuleSeverity.HIGH,
        applicable_phases=(AssessmentPhase.INITIAL,),
        resource_types=("AWS::S3::Bucket",),
        source_references=(
            SourceReference(source_id="isms-p", locator="5.2.1", content_sha256="digest"),
        ),
    )
    return PolicyContext(
        policy_profile_id="profile-001",
        policy_profile_version="v1",
        phase=AssessmentPhase.INITIAL,
        resource_type="AWS::S3::Bucket",
        rules=(rule,),
    )


class Evaluator:
    def __init__(self, result: EvaluationResult) -> None:
        self.result = result

    def evaluate(
        self, *, resource_id: str, rule: PolicyRule, context: PolicyContext
    ) -> EvaluationResult:
        return self.result


def result(rule_id: str = "S3-001") -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id=rule_id,
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.PASS,
        severity="HIGH",
        score=100,
        rationale="Block public access is enabled",
        evidence_references=("terraform:aws_s3_bucket_public_access_block",),
        rule_version="v1",
        rubric_version="mvp-v1",
    )


class AssessmentRunnerTest(unittest.TestCase):
    def test_evaluates_every_context_rule_and_returns_validated_result(self) -> None:
        outcomes = AssessmentRunner(Evaluator(result())).evaluate_resource(
            resource_id="bucket-001", context=context()
        )
        self.assertEqual(outcomes[0].rule_id, "S3-001")

    def test_rejects_evaluator_result_for_an_unapproved_rule(self) -> None:
        with self.assertRaises(EvaluationContractError):
            AssessmentRunner(Evaluator(result("EC2-001"))).evaluate_resource(
                resource_id="bucket-001", context=context()
            )
