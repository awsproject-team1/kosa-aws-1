"""Assessment runner rejects model output that escapes its Policy Context."""

import unittest
from dataclasses import replace

from apps.backend.assessment import (
    AssessmentRunner,
    BedrockEvaluationError,
    EvaluationContractError,
)
from apps.backend.policy import PolicyContext
from packages.contracts import (
    AssessmentPhase,
    DecisionSource,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    RuleSeverity,
    ScoringMode,
    SourceReference,
)

MODEL_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m0-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-s3-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m0-s3-v1",
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
            SourceReference(
                source_id="isms-p",
                source_version="2023-10-31",
                locator="5.2.1",
                content_sha256="digest",
            ),
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
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
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
        model_profile_id=MODEL_PROFILE.model_profile_id,
    )


def two_rule_context() -> PolicyContext:
    base = context()
    second = replace(base.rules[0], rule_id="S3-002")
    return replace(base, rules=(base.rules[0], second))


class FailingEvaluator:
    """Fails the first rule, answers the second. Mirrors one bad model response in a batch."""

    perspective = EvaluationPerspective.IAC

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def evaluate(self, *, resource_id, rule, context, model_profile):
        self.calls += 1
        if rule.rule_id == "S3-001":
            raise self.error
        return replace(result(), rule_id=rule.rule_id)


class OneFailedCoordinateTest(unittest.TestCase):
    """한 좌표의 실패가 나머지를 버리지 않는다 — 라이브에서 48h 동안 38,899회 실패한 그 고리다."""

    def test_a_failed_coordinate_becomes_execution_error_and_the_rest_survive(self) -> None:
        evaluator = FailingEvaluator(BedrockEvaluationError("evidence reference is outside"))

        outcomes = AssessmentRunner(evaluator).evaluate_resource(
            resource_id="bucket-001", context=two_rule_context(), model_profile=MODEL_PROFILE
        )

        self.assertEqual(evaluator.calls, 2)
        failed, judged = outcomes
        self.assertIs(failed.status, EvaluationStatus.EXECUTION_ERROR)
        self.assertIs(failed.perspective, EvaluationPerspective.IAC)
        self.assertIs(failed.decided_by, DecisionSource.CODE)
        self.assertEqual(failed.evidence_references, ())
        self.assertIs(judged.status, EvaluationStatus.PASS)

    def test_the_recorded_reason_names_the_failure_kind_not_the_rejected_value(self) -> None:
        """거부한 locator를 결과에 실으면 지어낸 값을 그대로 저장하는 셈이다."""
        evaluator = FailingEvaluator(
            BedrockEvaluationError("outside approved evidence: resource_document:main.tf#L26")
        )

        failed = AssessmentRunner(evaluator).evaluate_resource(
            resource_id="bucket-001", context=two_rule_context(), model_profile=MODEL_PROFILE
        )[0]

        self.assertIn("BedrockEvaluationError", failed.rationale)
        self.assertNotIn("resource_document", failed.rationale)

    def test_a_wiring_error_is_not_swallowed(self) -> None:
        """`TypeError`는 배선 오류다. 그것을 EXECUTION_ERROR로 덮으면 버그가 결과로 저장된다."""
        with self.assertRaises(TypeError):
            AssessmentRunner(
                FailingEvaluator(TypeError("evaluator is misconfigured"))
            ).evaluate_resource(
                resource_id="bucket-001", context=two_rule_context(), model_profile=MODEL_PROFILE
            )

    def test_without_a_known_perspective_the_failure_propagates(self) -> None:
        """어느 관점의 실패인지 말할 수 없으면 결과를 지어내지 않는다."""

        class Anonymous(FailingEvaluator):
            perspective = None

        with self.assertRaises(BedrockEvaluationError):
            AssessmentRunner(Anonymous(BedrockEvaluationError("no"))).evaluate_resource(
                resource_id="bucket-001", context=two_rule_context(), model_profile=MODEL_PROFILE
            )

    def test_an_explicit_perspective_overrides_the_evaluator(self) -> None:
        class Anonymous(FailingEvaluator):
            perspective = None

        failed = AssessmentRunner(
            Anonymous(BedrockEvaluationError("no")), perspective=EvaluationPerspective.AWS_ACTUAL
        ).evaluate_resource(
            resource_id="bucket-001", context=two_rule_context(), model_profile=MODEL_PROFILE
        )[0]

        self.assertIs(failed.perspective, EvaluationPerspective.AWS_ACTUAL)


class AssessmentRunnerTest(unittest.TestCase):
    def test_evaluates_every_context_rule_and_returns_validated_result(self) -> None:
        outcomes = AssessmentRunner(Evaluator(result())).evaluate_resource(
            resource_id="bucket-001", context=context(), model_profile=MODEL_PROFILE
        )
        self.assertEqual(outcomes[0].rule_id, "S3-001")

    def test_rejects_evaluator_result_for_an_unapproved_rule(self) -> None:
        with self.assertRaises(EvaluationContractError):
            AssessmentRunner(Evaluator(result("EC2-001"))).evaluate_resource(
                resource_id="bucket-001", context=context(), model_profile=MODEL_PROFILE
            )


class _EvidenceStubEvaluator:
    """Return a well-formed result whose evidence the runner must check."""

    def __init__(self, evidence_references: tuple[str, ...]) -> None:
        self._evidence_references = evidence_references

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        return EvaluationResult(
            resource_id=resource_id,
            rule_id=rule.rule_id,
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.FAIL,
            severity=rule.severity.value,
            score=10,
            rationale="stub",
            evidence_references=self._evidence_references,
            rule_version=rule.version,
            rubric_version=model_profile.rubric_version,
            model_profile_id=model_profile.model_profile_id,
            scoring_mode=ScoringMode.CONTINUOUS,
        )


class EvidenceBoundaryTest(unittest.TestCase):
    """평가기는 승인된 Policy Context 밖의 근거를 인용할 수 없다."""

    def setUp(self) -> None:
        self.context = context()
        self.reference = self.context.rules[0].source_references[0]

    def _run(self, evidence: tuple[str, ...]) -> tuple[EvaluationResult, ...]:
        return AssessmentRunner(_EvidenceStubEvaluator(evidence)).evaluate_resource(
            resource_id="bucket-001",
            context=self.context,
            model_profile=MODEL_PROFILE,
        )

    def test_accepts_canonical_policy_and_resource_evidence(self) -> None:
        results = self._run(
            (self.reference.evidence_reference, "aws:s3:bucket/bucket-001#read-resource")
        )

        self.assertEqual(len(results), len(self.context.rules))

    def test_rejects_versionless_policy_evidence(self) -> None:
        versionless = f"{self.reference.source_id}#{self.reference.locator}"

        with self.assertRaisesRegex(EvaluationContractError, "outside the approved policy context"):
            self._run((versionless,))

    def test_rejects_evidence_outside_the_context(self) -> None:
        with self.assertRaisesRegex(EvaluationContractError, "outside the approved policy context"):
            self._run(("other-source@v1#control/1.1.1",))
