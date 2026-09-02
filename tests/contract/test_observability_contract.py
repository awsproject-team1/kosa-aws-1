"""Demo-run observability gate items are present-or-unmet (ADR-0021 §3)."""

import unittest

from packages.contracts import (
    AssessmentSuccessMetric,
    AuditTrailMetric,
    BedrockUsageMetric,
    CostMetric,
    DemoRunObservability,
    JobResumptionMetric,
    ObservabilityGateItem,
    PlanApplyMetric,
    QueueHealthMetric,
)

_CAPTURED_AT = "2026-09-03T00:00:00+00:00"


def _passing_bedrock() -> BedrockUsageMetric:
    return BedrockUsageMetric(
        calls_by_role={"assessment": 12},
        tokens_by_role={"assessment": 34_000},
        p95_latency_ms_by_role={"assessment": 1800.0},
    )


def _passing_observability(**overrides: object) -> DemoRunObservability:
    fields: dict[str, object] = {
        "customer_id": "cust-1",
        "deployment_id": "dep-1",
        "captured_at": _CAPTURED_AT,
        "assessment_success": AssessmentSuccessMetric(planned_evaluations=18, execution_errors=0),
        "bedrock_usage": _passing_bedrock(),
        "queue_health": QueueHealthMetric(dlq_depth=0, max_queue_age_seconds=42),
        "job_resumption": JobResumptionMetric(
            checkpoint_resumptions=1, requeue_before_visibility_timeout_observed=True
        ),
        "plan_apply": PlanApplyMetric(plan_failures=0, apply_failures=0, unapproved_applies=0),
        "audit_trail": AuditTrailMetric(
            remediation_events=1,
            approval_events=1,
            apply_events=1,
            verification_events=1,
        ),
        "cost": CostMetric(currency="USD", bedrock_cost=1.2, lambda_cost=0.3, storage_cost=0.1),
        "sensitive_data_absent_verified": True,
    }
    fields.update(overrides)
    return DemoRunObservability(**fields)


class ObservabilityGateItemContractTest(unittest.TestCase):
    def test_covers_the_seven_adr_0021_gate_items(self) -> None:
        self.assertEqual(
            {member.value for member in ObservabilityGateItem},
            {
                "ASSESSMENT_SUCCESS",
                "BEDROCK_USAGE",
                "QUEUE_HEALTH",
                "JOB_RESUMPTION",
                "PLAN_APPLY",
                "AUDIT_TRAIL",
                "COST",
            },
        )


class AssessmentSuccessMetricTest(unittest.TestCase):
    def test_no_errors_over_planned_work_meets_gate(self) -> None:
        self.assertTrue(
            AssessmentSuccessMetric(planned_evaluations=10, execution_errors=0).meets_gate
        )

    def test_any_execution_error_fails_the_gate(self) -> None:
        self.assertFalse(
            AssessmentSuccessMetric(planned_evaluations=10, execution_errors=1).meets_gate
        )

    def test_no_planned_work_is_not_a_pass(self) -> None:
        self.assertFalse(
            AssessmentSuccessMetric(planned_evaluations=0, execution_errors=0).meets_gate
        )

    def test_more_errors_than_planned_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AssessmentSuccessMetric(planned_evaluations=1, execution_errors=2)


class BedrockUsageMetricTest(unittest.TestCase):
    def test_recorded_calls_meet_gate_and_total_sums_roles(self) -> None:
        metric = BedrockUsageMetric(
            calls_by_role={"assessment": 3, "remediation": 2},
            tokens_by_role={"assessment": 100, "remediation": 50},
            p95_latency_ms_by_role={"assessment": 900.0, "remediation": 1200.0},
        )
        self.assertTrue(metric.meets_gate)
        self.assertEqual(metric.total_calls, 5)

    def test_no_calls_fails_the_gate(self) -> None:
        self.assertFalse(
            BedrockUsageMetric(
                calls_by_role={}, tokens_by_role={}, p95_latency_ms_by_role={}
            ).meets_gate
        )

    def test_role_sets_must_agree_across_the_three_maps(self) -> None:
        with self.assertRaises(ValueError):
            BedrockUsageMetric(
                calls_by_role={"assessment": 1},
                tokens_by_role={"assessment": 1, "remediation": 1},
                p95_latency_ms_by_role={"assessment": 1.0},
            )


class QueueHealthMetricTest(unittest.TestCase):
    def test_empty_dlq_meets_gate_regardless_of_age(self) -> None:
        self.assertTrue(QueueHealthMetric(dlq_depth=0, max_queue_age_seconds=999).meets_gate)

    def test_any_dlq_depth_fails_the_gate(self) -> None:
        self.assertFalse(QueueHealthMetric(dlq_depth=1, max_queue_age_seconds=0).meets_gate)


class PlanApplyMetricTest(unittest.TestCase):
    def test_clean_plan_and_apply_meet_gate(self) -> None:
        self.assertTrue(
            PlanApplyMetric(plan_failures=0, apply_failures=0, unapproved_applies=0).meets_gate
        )

    def test_an_unapproved_apply_fails_the_gate(self) -> None:
        self.assertFalse(
            PlanApplyMetric(plan_failures=0, apply_failures=0, unapproved_applies=1).meets_gate
        )


class AuditTrailMetricTest(unittest.TestCase):
    def test_all_four_event_kinds_present_meets_gate(self) -> None:
        self.assertTrue(
            AuditTrailMetric(
                remediation_events=1,
                approval_events=1,
                apply_events=1,
                verification_events=1,
            ).meets_gate
        )

    def test_a_missing_event_kind_fails_the_gate(self) -> None:
        self.assertFalse(
            AuditTrailMetric(
                remediation_events=1,
                approval_events=1,
                apply_events=1,
                verification_events=0,
            ).meets_gate
        )


class CostMetricTest(unittest.TestCase):
    def test_present_costs_meet_gate_and_total_sums(self) -> None:
        metric = CostMetric(currency="USD", bedrock_cost=1.0, lambda_cost=2.0, storage_cost=3.0)
        self.assertTrue(metric.meets_gate)
        self.assertEqual(metric.total_cost, 6.0)

    def test_infinite_cost_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CostMetric(
                currency="USD",
                bedrock_cost=float("inf"),
                lambda_cost=0.0,
                storage_cost=0.0,
            )


class DemoRunObservabilityTest(unittest.TestCase):
    def test_all_items_present_and_verified_meets_gate(self) -> None:
        record = _passing_observability()
        self.assertTrue(record.meets_gate)
        self.assertEqual(record.unmet_items(), ())

    def test_unmet_item_is_enumerated_and_blocks_the_gate(self) -> None:
        record = _passing_observability(
            queue_health=QueueHealthMetric(dlq_depth=3, max_queue_age_seconds=10)
        )
        self.assertFalse(record.meets_gate)
        self.assertIn(ObservabilityGateItem.QUEUE_HEALTH, record.unmet_items())

    def test_unverified_sensitive_data_absence_blocks_the_gate(self) -> None:
        record = _passing_observability(sensitive_data_absent_verified=False)
        self.assertFalse(record.meets_gate)
        # 값이 모두 존재해도 민감 원문 부재 확인이 없으면 통과가 아니다.
        self.assertEqual(record.unmet_items(), ())

    def test_to_dict_exposes_gate_result_and_unmet_items(self) -> None:
        payload = _passing_observability(
            plan_apply=PlanApplyMetric(plan_failures=0, apply_failures=1, unapproved_applies=0)
        ).to_dict()
        self.assertFalse(payload["meets_gate"])
        self.assertEqual(payload["unmet_items"], ["PLAN_APPLY"])
        self.assertEqual(payload["captured_at"], _CAPTURED_AT)

    def test_naive_captured_at_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _passing_observability(captured_at="2026-09-03T00:00:00")


if __name__ == "__main__":
    unittest.main()
