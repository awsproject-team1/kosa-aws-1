"""An EXECUTION_ERROR names its reason without repeating what the model wrote.

라이브에서 `EXECUTION_ERROR` 8건이 이유 없이 남았다 — rationale에 예외 **종류**만 있었고 로그에도
사유가 없어, 재생해서야 근거 표기 문제였음이 드러났다(`docs/evaluations/data/
iac-evidence-shape-20260905.md`). 사유의 고정 문구는 실어야 하고, 모델이 쓴 문자열은 실으면 안
된다. 이 파일은 그 경계를 고정한다.
"""

import unittest

from apps.backend.assessment import BedrockEvaluationError
from apps.backend.assessment.runner import _execution_error
from packages.contracts import (
    AssessmentPhase,
    DecisionSource,
    EvaluationPerspective,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleSeverity,
    SourceReference,
)

PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v3",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-three-perspective-rubric-v3",
    rubric_version="m1-three-perspective-v1",
    golden_dataset_version="g",
)
RULE = PolicyRule(
    rule_id="ISMSP-ALB_ACCESS_LOGGING",
    version="2023-10-31",
    title="ISMS-P Load balancers record access logs",
    severity=RuleSeverity.MEDIUM,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::ElasticLoadBalancingV2::LoadBalancer",),
    source_references=(
        SourceReference(
            source_id="isms-p-2023",
            source_version="2023-10-31",
            locator="control/2.9.4",
            content_sha256="a" * 64,
        ),
    ),
)


def _error(error: Exception):
    return _execution_error(
        resource_id="arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/x/1",
        rule=RULE,
        perspective=EvaluationPerspective.IAC,
        model_profile=PROFILE,
        error=error,
    )


class ExecutionErrorRationaleTest(unittest.TestCase):
    def test_the_fixed_reason_is_recorded(self) -> None:
        result = _error(BedrockEvaluationError("evidence_references must be a non-empty string"))

        self.assertIs(result.status, EvaluationStatus.EXECUTION_ERROR)
        self.assertIs(result.decided_by, DecisionSource.CODE)
        self.assertIn(
            "BedrockEvaluationError (evidence_references must be a non-empty string)",
            result.rationale,
        )
        self.assertEqual(result.evidence_references, ())

    def test_model_written_text_after_the_colon_is_dropped(self) -> None:
        """콜론 뒤는 모델이 지어낸 locator다. 거부한 값을 결과에 저장하지 않는다."""
        invented = "resource_document:main.tf#L26-L30"
        result = _error(
            BedrockEvaluationError(f"evidence reference is outside approved evidence: {invented}")
        )

        self.assertIn("(evidence reference is outside approved evidence)", result.rationale)
        self.assertNotIn(invented, result.rationale)

    def test_an_error_without_a_message_still_names_its_type(self) -> None:
        result = _error(ValueError())

        self.assertIn("The evaluation did not complete: ValueError.", result.rationale)

    def test_the_reason_is_logged_with_the_coordinate(self) -> None:
        with self.assertLogs("governance.assessment", level="WARNING") as logs:
            _error(BedrockEvaluationError("Bedrock response is not JSON"))

        line = "\n".join(logs.output)
        self.assertIn("ISMSP-ALB_ACCESS_LOGGING", line)
        self.assertIn("IAC", line)
        self.assertIn("Bedrock response is not JSON", line)


if __name__ == "__main__":
    unittest.main()
