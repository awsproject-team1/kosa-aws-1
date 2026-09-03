"""The authoring pipeline is additive: existing Runtime contracts do not move.

R1이 이 파일의 전부다. 정책 문서에서 Rule을 만드는 경로를 붙이면서 **기존 평가 결과 계약을
바꾸지 않는다.** 바꾸면 이미 저장된 결과와 새 결과가 같은 의미를 갖지 않게 되고, 그 둘을
비교하는 Post-Deploy Verification이 조용히 무의미해진다.

여기서 고정하는 것은 값의 목록이지 그 값의 쓰임이 아니다. 목록이 움직였다는 사실 자체를
변경자가 의도적으로 마주하게 만드는 것이 목적이다.
"""

import unittest
from pathlib import Path

from apps.backend.assessment.execution_plan import EvaluationExecutionPlanner
from apps.backend.assessment.readiness import _NON_SCORING_PERSPECTIVES
from apps.backend.policy.registry import load_rule_registry
from packages.contracts import (
    SCORE_ANCHORS,
    EvaluationPerspective,
    EvaluationStatus,
    RuleSeverity,
    ScoringMode,
)
from packages.contracts.assessments import EvaluationResult

RULES_PATH = Path(__file__).parents[2] / "fixtures" / "rules"


def _result(**overrides: object) -> EvaluationResult:
    fields: dict[str, object] = {
        "resource_id": "resource-1",
        "rule_id": "RULE-1",
        "perspective": EvaluationPerspective.AWS_ACTUAL,
        "status": EvaluationStatus.PASS,
        "severity": RuleSeverity.HIGH.value,
        "score": 95.0,
        "rationale": "rationale",
        "evidence_references": ("aws:s3:bucket/b#read-resource",),
        "rule_version": "v1",
        "rubric_version": "rubric/1",
        "model_profile_id": "assessment-v1",
    }
    fields.update(overrides)
    return EvaluationResult(**fields)  # type: ignore[arg-type]


class EvaluationOutcomeContractTest(unittest.TestCase):
    def test_the_evaluation_status_value_set_is_unchanged(self) -> None:
        """`EVALUATED`나 `PARTIAL`을 이번 범위에서 추가하지 않는다."""
        self.assertEqual(
            sorted(value.value for value in EvaluationStatus),
            [
                "EXECUTION_ERROR",
                "FAIL",
                "INSUFFICIENT_EVIDENCE",
                "MANUAL_REVIEW",
                "OUT_OF_SCOPE",
                "PASS",
            ],
        )

    def test_the_scoring_mode_and_anchors_are_unchanged(self) -> None:
        self.assertEqual(sorted(value.value for value in ScoringMode), ["ANCHORED", "CONTINUOUS"])
        self.assertEqual(SCORE_ANCHORS, frozenset({0, 15, 30, 50, 70, 85, 100}))

    def test_score_is_still_a_continuous_float_over_the_full_range(self) -> None:
        """정수 강제나 anchor 강제를 도입하지 않는다.

        annotation이 아니라 실제로 받아들이는 값으로 확인한다 — 계약을 지키는 것은 타입
        주석이 아니라 검증 코드다.
        """
        for score in (0.0, 37.5, 63.25, 100.0):
            with self.subTest(score=score):
                self.assertEqual(_result(score=score).score, score)

    def test_anchored_scoring_still_refuses_a_non_anchor_score(self) -> None:
        """`ScoringMode.ANCHORED`의 의미가 바뀌지 않았는지 함께 고정한다."""
        with self.assertRaisesRegex(ValueError, "anchored score"):
            _result(score=37.5, scoring_mode=ScoringMode.ANCHORED)

    def test_the_result_carries_no_judgment_field(self) -> None:
        for forbidden in ("judgment", "source_score", "anchor"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, EvaluationResult.__dataclass_fields__)

    def test_severity_stays_a_required_non_nullable_string(self) -> None:
        """Evidence 기반 동적 severity나 nullable severity를 도입하지 않는다."""
        self.assertEqual(
            sorted(value.value for value in RuleSeverity), ["CRITICAL", "HIGH", "LOW", "MEDIUM"]
        )
        with self.assertRaises((TypeError, ValueError)):
            _result(severity=None)


class PerspectiveContractTest(unittest.TestCase):
    def test_manual_is_the_only_addition_to_the_perspective_set(self) -> None:
        self.assertEqual(
            sorted(value.value for value in EvaluationPerspective),
            ["AWS_ACTUAL", "DRIFT", "IAC", "MANUAL"],
        )

    def test_only_drift_and_manual_are_excluded_from_the_numeric_average(self) -> None:
        """기존 DRIFT 제외는 그대로이고, MANUAL만 새로 더해진다.

        제외 기준은 Perspective이지 status가 아니다 — IAC/AWS_ACTUAL의 `MANUAL_REVIEW`는
        지금처럼 점수에 들어간다.
        """
        self.assertEqual(
            _NON_SCORING_PERSPECTIVES,
            frozenset({EvaluationPerspective.DRIFT, EvaluationPerspective.MANUAL}),
        )


class LegacyRuleBehaviourTest(unittest.TestCase):
    def test_every_committed_fixture_rule_still_loads(self) -> None:
        registry = load_rule_registry(RULES_PATH)

        self.assertEqual(len(registry.rules), 16)

    def test_a_legacy_rule_keeps_the_three_original_perspectives(self) -> None:
        """`evaluation_type is None`인 Rule의 실행 계획은 이 변경으로 바뀌지 않는다."""
        registry = load_rule_registry(RULES_PATH)
        planner = EvaluationExecutionPlanner(
            available_perspectives=(
                EvaluationPerspective.IAC,
                EvaluationPerspective.AWS_ACTUAL,
            ),
            derive_drift=True,
        )

        for rule in registry.rules:
            with self.subTest(rule=rule.rule_id):
                self.assertTrue(rule.is_legacy)
                self.assertEqual(
                    planner.perspectives_for(rule),
                    (
                        EvaluationPerspective.IAC,
                        EvaluationPerspective.AWS_ACTUAL,
                        EvaluationPerspective.DRIFT,
                    ),
                )

    def test_every_legacy_rule_stays_drift_eligible(self) -> None:
        """기존 Drift 대상 집합이 줄어들면, 지금까지 잡히던 불일치가 조용히 사라진다."""
        registry = load_rule_registry(RULES_PATH)
        planner = EvaluationExecutionPlanner(
            available_perspectives=(
                EvaluationPerspective.IAC,
                EvaluationPerspective.AWS_ACTUAL,
            ),
            derive_drift=True,
        )

        self.assertEqual(len(planner.drift_rules(registry.rules)), len(registry.rules))


if __name__ == "__main__":
    unittest.main()
