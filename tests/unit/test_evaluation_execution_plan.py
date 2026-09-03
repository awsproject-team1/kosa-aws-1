"""Which perspectives a Rule produces must have exactly one answer.

계획된 좌표와 실제 평가가 다른 곳에서 결정되면 두 가지가 생긴다: 채워지지 않는 좌표(coverage가
영원히 완료되지 않음)와 계획에 없는 결과(저장되지 못함). 그래서 이 helper가 유일한 답이다.

그리고 **legacy Rule의 동작은 바뀌지 않는다.** `evaluation_type is None`인 커밋된 fixture Rule은
지금까지처럼 IAC + AWS_ACTUAL + DRIFT를 만든다.
"""

import unittest

from apps.backend.assessment.execution_plan import (
    PERSPECTIVE_ORDER,
    EvaluationExecutionPlanner,
    evaluated_perspectives,
    is_drift_eligible,
)
from packages.contracts import (
    AssessmentPhase,
    EvaluationPerspective,
    PolicyRule,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)

REFERENCE = SourceReference(
    source_id="internal-policy",
    source_version="2026.1",
    locator="section/3.1",
    content_sha256="a" * 64,
)

IAC = EvaluationPerspective.IAC
AWS = EvaluationPerspective.AWS_ACTUAL
DRIFT = EvaluationPerspective.DRIFT
MANUAL = EvaluationPerspective.MANUAL


def rule(evaluation_type: RuleEvaluationType | None, *, rule_id: str = "RULE-1") -> PolicyRule:
    fields: dict[str, object] = {
        "rule_id": rule_id,
        "version": "2026.1",
        "title": "A rule",
        "severity": RuleSeverity.HIGH,
        "applicable_phases": (AssessmentPhase.INITIAL,),
        "resource_types": ("AWS::S3::Bucket",),
        "source_references": (REFERENCE,),
    }
    if evaluation_type is not None:
        fields.update(
            control_key="S3_BLOCK_PUBLIC_ACCESS",
            control_catalog_version="governance-control-catalog/2026-09-03",
            evaluation_type=evaluation_type,
        )
        if evaluation_type is not RuleEvaluationType.MANUAL:
            fields.update(
                evaluation_rubric="Fail when public access is not blocked.",
                required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
            )
    return PolicyRule(**fields)  # type: ignore[arg-type]


def _planner(*, derive_drift: bool = True, available=(IAC, AWS)) -> EvaluationExecutionPlanner:
    return EvaluationExecutionPlanner(available_perspectives=available, derive_drift=derive_drift)


class ExecutionTableTest(unittest.TestCase):
    def test_the_table_matches_the_documented_execution_plan(self) -> None:
        self.assertEqual(evaluated_perspectives(rule(None)), (IAC, AWS))
        self.assertEqual(evaluated_perspectives(rule(RuleEvaluationType.IAC)), (IAC,))
        self.assertEqual(evaluated_perspectives(rule(RuleEvaluationType.AWS)), (AWS,))
        self.assertEqual(evaluated_perspectives(rule(RuleEvaluationType.HYBRID)), (IAC, AWS))
        self.assertEqual(evaluated_perspectives(rule(RuleEvaluationType.MANUAL)), (MANUAL,))

    def test_a_legacy_rule_keeps_its_three_perspectives(self) -> None:
        """`evaluation_type is None`인 커밋된 Rule의 동작은 이 변경으로 바뀌지 않는다."""
        planner = _planner()

        self.assertEqual(planner.perspectives_for(rule(None)), (IAC, AWS, DRIFT))

    def test_a_hybrid_rule_matches_the_legacy_shape(self) -> None:
        planner = _planner()

        self.assertEqual(
            planner.perspectives_for(rule(RuleEvaluationType.HYBRID)), (IAC, AWS, DRIFT)
        )

    def test_a_single_perspective_rule_is_never_sent_to_drift(self) -> None:
        """한쪽만 평가하는 것이 그 Rule의 정의다.

        Drift가 없는 쪽을 "누락된 Perspective"로 읽으면 `MANUAL_REVIEW`가 생기고, 그것은 실제
        불일치와 구별되지 않는다.
        """
        planner = _planner()

        self.assertEqual(planner.perspectives_for(rule(RuleEvaluationType.IAC)), (IAC,))
        self.assertEqual(planner.perspectives_for(rule(RuleEvaluationType.AWS)), (AWS,))
        self.assertFalse(is_drift_eligible(rule(RuleEvaluationType.IAC)))
        self.assertFalse(is_drift_eligible(rule(RuleEvaluationType.AWS)))

    def test_drift_rules_exclude_single_perspective_rules(self) -> None:
        rules = (
            rule(None, rule_id="LEGACY"),
            rule(RuleEvaluationType.HYBRID, rule_id="HYBRID"),
            rule(RuleEvaluationType.IAC, rule_id="IAC-ONLY"),
            rule(RuleEvaluationType.AWS, rule_id="AWS-ONLY"),
        )
        planner = _planner()

        self.assertEqual(
            [entry.rule_id for entry in planner.drift_rules(rules)], ["LEGACY", "HYBRID"]
        )


class WorkerCapabilityTest(unittest.TestCase):
    def test_a_perspective_the_worker_cannot_run_is_not_planned(self) -> None:
        """계획만 하고 실행하지 못하면 coverage가 영원히 완료되지 않는다."""
        planner = _planner(derive_drift=False, available=(IAC,))

        self.assertEqual(planner.perspectives_for(rule(RuleEvaluationType.AWS)), ())
        self.assertEqual(planner.perspectives_for(rule(None)), (IAC,))

    def test_a_legacy_rule_on_an_iac_only_worker_produces_no_drift(self) -> None:
        planner = _planner(derive_drift=False, available=(IAC,))

        self.assertNotIn(DRIFT, planner.perspectives_for(rule(None)))

    def test_deriving_drift_requires_both_runners(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires both IAC and AWS_ACTUAL"):
            EvaluationExecutionPlanner(available_perspectives=(IAC,), derive_drift=True)

    def test_drift_is_never_an_available_runner(self) -> None:
        with self.assertRaisesRegex(ValueError, "DRIFT is derived"):
            EvaluationExecutionPlanner(available_perspectives=(IAC, AWS, DRIFT))


class RuleSubsetTest(unittest.TestCase):
    def test_each_perspective_receives_only_the_rules_it_evaluates(self) -> None:
        """전체를 넘기면 IaC 전용 Rule이 Actual 평가기에도 들어가 볼 수 없는 것을 판정한다."""
        rules = (
            rule(RuleEvaluationType.IAC, rule_id="IAC-ONLY"),
            rule(RuleEvaluationType.AWS, rule_id="AWS-ONLY"),
            rule(RuleEvaluationType.HYBRID, rule_id="HYBRID"),
        )
        planner = _planner()

        self.assertEqual(
            [entry.rule_id for entry in planner.rules_for(IAC, rules)], ["IAC-ONLY", "HYBRID"]
        )
        self.assertEqual(
            [entry.rule_id for entry in planner.rules_for(AWS, rules)], ["AWS-ONLY", "HYBRID"]
        )

    def test_the_subset_preserves_profile_order(self) -> None:
        rules = (
            rule(RuleEvaluationType.HYBRID, rule_id="SECOND"),
            rule(RuleEvaluationType.HYBRID, rule_id="FIRST"),
        )

        self.assertEqual(
            [entry.rule_id for entry in _planner().rules_for(IAC, rules)], ["SECOND", "FIRST"]
        )

    def test_planned_perspectives_are_reported_in_canonical_order(self) -> None:
        rules = (
            rule(RuleEvaluationType.AWS, rule_id="AWS-ONLY"),
            rule(RuleEvaluationType.IAC, rule_id="IAC-ONLY"),
            rule(RuleEvaluationType.HYBRID, rule_id="HYBRID"),
        )

        planned = _planner().planned_perspectives(rules)

        self.assertEqual(planned, (IAC, AWS, DRIFT))
        self.assertEqual(
            list(planned), [value for value in PERSPECTIVE_ORDER if value in set(planned)]
        )


class ManualRuleTest(unittest.TestCase):
    def test_a_manual_rule_produces_only_the_manual_perspective(self) -> None:
        planner = EvaluationExecutionPlanner(available_perspectives=(MANUAL,))

        self.assertEqual(planner.perspectives_for(rule(RuleEvaluationType.MANUAL)), (MANUAL,))

    def test_a_manual_rule_is_never_drift_eligible(self) -> None:
        self.assertFalse(is_drift_eligible(rule(RuleEvaluationType.MANUAL)))

    def test_a_worker_without_a_manual_runner_plans_no_manual_coordinate(self) -> None:
        planner = _planner()

        self.assertEqual(planner.perspectives_for(rule(RuleEvaluationType.MANUAL)), ())


if __name__ == "__main__":
    unittest.main()
