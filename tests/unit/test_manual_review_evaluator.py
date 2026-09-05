"""A MANUAL Rule becomes a recorded coordinate, not a model's guess.

두 가지가 이 evaluator의 요점이다.

1. **아무것도 호출하지 않는다.** 사람이 검토해야 한다고 승인된 통제에 모델을 부르면, 그 결과는
   도구가 관찰한 사실이 아니라 추측이면서 다른 결과와 똑같이 생겼다.
2. **좌표는 Repository 단위로 안정적이다.** Assessment ID를 쓰면 Initial과 Verification이 서로
   다른 좌표를 갖고, 정확히 비교하려고 만든 결과가 비교를 불가능하게 만든다.

그리고 readiness에서 **숫자 평균만** 제외한다. Coverage와 plan 완료에는 그대로 들어간다 —
빼버리면 그 통제가 존재한다는 사실 자체가 결과에서 사라진다.
"""

import unittest

from apps.backend.assessment.manual_review import (
    MANUAL_REVIEW_RATIONALE,
    NOT_YET_SUPPORTED_RATIONALE,
    ManualReviewEvaluator,
    governance_resource_id,
)
from apps.backend.assessment.readiness import calculate_readiness_score
from apps.backend.policy import PolicyContext
from apps.backend.policy.control_catalog import (
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
    NOT_YET_SUPPORTED_CONTROL_KEY,
)
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    ModelProfile,
    ModelProfileRole,
    PlannedEvaluation,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

REFERENCE = SourceReference(
    source_id="internal-policy",
    source_version="2026.1",
    locator="heading/governance/item/1",
    content_sha256="a" * 64,
)

MODEL_PROFILE = ModelProfile(
    model_profile_id="assessment-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment/1",
    rubric_version="rubric/1",
    golden_dataset_version="golden/1",
)

MANUAL_RULE = PolicyRule(
    rule_id="CUST-ORGANIZATIONAL_CONTROL_MANUAL_REVIEW-0123456789ab",
    version="2026.1",
    title="Annual processor agreement review",
    severity=RuleSeverity.MEDIUM,
    applicable_phases=(AssessmentPhase.INITIAL, AssessmentPhase.POST_DEPLOY_VERIFICATION),
    resource_types=(GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,),
    source_references=(REFERENCE,),
    control_key=MANUAL_CONTROL_KEY,
    control_catalog_version="governance-control-catalog/2026-09-03",
    evaluation_type=RuleEvaluationType.MANUAL,
)

AUTOMATED_RULE = PolicyRule(
    rule_id="CUST-S3_BLOCK_PUBLIC_ACCESS-0123456789ab",
    version="2026.1",
    title="Buckets block public access",
    severity=RuleSeverity.CRITICAL,
    applicable_phases=(AssessmentPhase.INITIAL,),
    resource_types=("AWS::S3::Bucket",),
    source_references=(REFERENCE,),
    control_key="S3_BLOCK_PUBLIC_ACCESS",
    control_catalog_version="governance-control-catalog/2026-09-03",
    evaluation_type=RuleEvaluationType.AWS,
    evaluation_rubric="Fail when any block-public-access flag is false.",
    required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
)


def _context(
    *, resource_type: str = GOVERNANCE_ASSESSMENT_RESOURCE_TYPE, rules=(MANUAL_RULE,)
) -> PolicyContext:
    return PolicyContext(
        policy_profile_id="profile-customer-baseline",
        policy_profile_version="v1",
        phase=AssessmentPhase.INITIAL,
        resource_type=resource_type,
        rules=rules,
    )


class GovernanceCoordinateTest(unittest.TestCase):
    def test_the_coordinate_is_stable_for_one_repository(self) -> None:
        """Assessment ID를 쓰면 Initial과 Verification의 좌표가 달라져 비교가 깨진다."""
        self.assertEqual(governance_resource_id("repo-001"), "governance:repo-001")
        self.assertEqual(governance_resource_id("repo-001"), governance_resource_id("repo-001"))
        self.assertNotEqual(governance_resource_id("repo-001"), governance_resource_id("repo-002"))

    def test_a_blank_repository_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            governance_resource_id("   ")


class ManualEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = ManualReviewEvaluator()

    def _evaluate(self, rule: PolicyRule = MANUAL_RULE, **kwargs: object) -> EvaluationResult:
        return self.evaluator.evaluate(
            resource_id=governance_resource_id("repo-001"),
            rule=rule,
            context=kwargs.pop("context", _context()),  # type: ignore[arg-type]
            model_profile=MODEL_PROFILE,
        )

    def test_the_result_is_a_manual_review_coordinate(self) -> None:
        result = self._evaluate()

        self.assertIs(result.perspective, EvaluationPerspective.MANUAL)
        self.assertIs(result.status, EvaluationStatus.MANUAL_REVIEW)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.rationale, MANUAL_REVIEW_RATIONALE)
        self.assertEqual(result.rule_version, MANUAL_RULE.version)

    def test_severity_stays_the_rule_severity(self) -> None:
        """검토되지 않았다는 사실이 그 통제의 중요도를 바꾸지는 않는다."""
        self.assertEqual(self._evaluate().severity, RuleSeverity.MEDIUM.value)

    def test_the_evidence_is_the_policy_source_the_rule_cites(self) -> None:
        """도구가 관찰한 것이 없으므로 그 외의 Evidence를 붙이면 없는 관찰을 주장하게 된다."""
        result = self._evaluate()

        self.assertEqual(result.evidence_references, (REFERENCE.evidence_reference,))
        self.assertTrue(_context().allows_evidence(result.evidence_references[0]))

    def test_an_automated_rule_is_refused(self) -> None:
        """자동 평가 가능한 Rule을 사람 검토로 흘리면, 검사할 수 있었던 것이 검사되지 않는다."""
        with self.assertRaisesRegex(ValueError, "only accepts MANUAL rules"):
            self._evaluate(AUTOMATED_RULE)

    def test_a_real_resource_context_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "governance assessment resource only"):
            self._evaluate(context=_context(resource_type="AWS::S3::Bucket"))

    def test_the_rationale_does_not_change_between_runs(self) -> None:
        """실행마다 문장이 달라지면 같은 상태가 서로 다른 결과처럼 보인다."""
        self.assertEqual(self._evaluate().rationale, self._evaluate().rationale)


class ReadinessTreatmentTest(unittest.TestCase):
    def _result(
        self,
        *,
        perspective: EvaluationPerspective,
        status: EvaluationStatus,
        score: float,
        rule_id: str,
    ) -> EvaluationResult:
        return EvaluationResult(
            resource_id="resource-1",
            rule_id=rule_id,
            perspective=perspective,
            status=status,
            severity=RuleSeverity.HIGH.value,
            score=score,
            rationale="rationale",
            evidence_references=("aws:s3:bucket/b#read-resource",),
            rule_version="v1",
            rubric_version="rubric/1",
            model_profile_id="assessment-v1",
        )

    def test_a_manual_result_is_excluded_from_the_numeric_average(self) -> None:
        """0점이 평균을 끌어내리면 그 숫자는 "미검토"가 아니라 "위반"으로 읽힌다."""
        automated = self._result(
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.PASS,
            score=100.0,
            rule_id="RULE-AUTOMATED",
        )
        manual = self._result(
            perspective=EvaluationPerspective.MANUAL,
            status=EvaluationStatus.MANUAL_REVIEW,
            score=0.0,
            rule_id="RULE-MANUAL",
        )
        planned = tuple(
            PlannedEvaluation(
                resource_id="resource-1", rule_id=result.rule_id, perspective=result.perspective
            )
            for result in (automated, manual)
        )

        readiness = calculate_readiness_score(
            results=(automated, manual), planned_evaluations=planned
        )

        assert readiness is not None
        self.assertEqual(readiness.score, 100.0)
        self.assertEqual(readiness.evaluated_evaluations, 1)

    def test_a_manual_result_still_completes_the_plan(self) -> None:
        """빼버리면 그 통제가 존재한다는 사실 자체가 결과에서 사라진다."""
        automated = self._result(
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.PASS,
            score=100.0,
            rule_id="RULE-AUTOMATED",
        )
        planned = (
            PlannedEvaluation(
                resource_id="resource-1",
                rule_id="RULE-AUTOMATED",
                perspective=EvaluationPerspective.AWS_ACTUAL,
            ),
            PlannedEvaluation(
                resource_id="resource-1",
                rule_id="RULE-MANUAL",
                perspective=EvaluationPerspective.MANUAL,
            ),
        )

        # MANUAL 좌표가 계획됐지만 결과가 없으면 plan은 완료되지 않는다.
        self.assertIsNone(
            calculate_readiness_score(results=(automated,), planned_evaluations=planned)
        )

    def test_manual_review_on_an_automated_perspective_is_undetermined_not_scored(self) -> None:
        """IAC/AWS_ACTUAL의 `MANUAL_REVIEW`도 판정이 아니다 — 0점이 아니라 미판정으로 센다.

        0점으로 평균에 넣으면 "사람이 봐야 함"이 "위반"과 같은 무게로 준비도를 깎는다.
        """
        undetermined = self._result(
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.MANUAL_REVIEW,
            score=0.0,
            rule_id="RULE-AUTOMATED",
        )
        judged = self._result(
            perspective=EvaluationPerspective.AWS_ACTUAL,
            status=EvaluationStatus.PASS,
            score=100.0,
            rule_id="RULE-JUDGED",
        )
        planned = tuple(
            PlannedEvaluation(
                resource_id="resource-1", rule_id=result.rule_id, perspective=result.perspective
            )
            for result in (undetermined, judged)
        )

        readiness = calculate_readiness_score(
            results=(undetermined, judged), planned_evaluations=planned
        )

        assert readiness is not None
        self.assertEqual(readiness.score, 100.0)
        self.assertEqual(readiness.evaluated_evaluations, 1)
        self.assertEqual(readiness.undetermined_evaluations, 1)


if __name__ == "__main__":
    unittest.main()


class NotYetSupportedTest(unittest.TestCase):
    """A technical control the catalog cannot evidence yet is settled by a person — and says so."""

    def test_the_rationale_names_the_missing_capability_as_the_reason(self) -> None:
        from dataclasses import replace

        rule = replace(MANUAL_RULE, control_key=NOT_YET_SUPPORTED_CONTROL_KEY)
        context = PolicyContext(
            policy_profile_id="profile",
            policy_profile_version="v1",
            phase=AssessmentPhase.INITIAL,
            resource_type=GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
            rules=(rule,),
        )

        result = ManualReviewEvaluator().evaluate(
            resource_id=governance_resource_id("repo-001"),
            rule=rule,
            context=context,
            model_profile=MODEL_PROFILE,
        )

        self.assertIs(result.status, EvaluationStatus.MANUAL_REVIEW)
        self.assertEqual(result.rationale, NOT_YET_SUPPORTED_RATIONALE)
        self.assertTrue(result.rationale.startswith("Not yet supported"))
