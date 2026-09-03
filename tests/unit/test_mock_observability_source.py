"""M4 A 관측·비용 Mock source와 조립기의 결합 (ADR-0021 §3)."""

import unittest

from agent.runtime import (
    MockDemoRunMetricsSource,
    ObservabilitySourceError,
    ObservabilitySourceScopeError,
)
from apps.backend.api.observability import (
    DemoRunObservabilityService,
    ObservabilityAssemblyError,
)
from apps.backend.auth import Principal, Role
from packages.contracts import (
    AssessmentSuccessMetric,
    AuditTrailMetric,
    BedrockUsageMetric,
    CostMetric,
    DemoRunObservability,
    JobResumptionMetric,
    PlanApplyMetric,
    QueueHealthMetric,
)

CUSTOMER = "cust-001"
DEPLOYMENT = "dep-001"
CAPTURED_AT = "2026-09-03T00:00:00+00:00"


def _admin(customer_id: str = CUSTOMER) -> Principal:
    return Principal(
        subject="admin-001",
        client_id="client-001",
        customer_id=customer_id,
        roles=frozenset({Role.ADMIN}),
    )


def _record(
    *, customer_id: str = CUSTOMER, deployment_id: str = DEPLOYMENT
) -> DemoRunObservability:
    return DemoRunObservability(
        customer_id=customer_id,
        deployment_id=deployment_id,
        captured_at=CAPTURED_AT,
        assessment_success=AssessmentSuccessMetric(planned_evaluations=18, execution_errors=0),
        bedrock_usage=BedrockUsageMetric(
            calls_by_role={"assessment": 12},
            tokens_by_role={"assessment": 34_000},
            p95_latency_ms_by_role={"assessment": 1800.0},
        ),
        queue_health=QueueHealthMetric(dlq_depth=0, max_queue_age_seconds=42),
        job_resumption=JobResumptionMetric(
            checkpoint_resumptions=1, requeue_before_visibility_timeout_observed=True
        ),
        plan_apply=PlanApplyMetric(plan_failures=0, apply_failures=0, unapproved_applies=0),
        audit_trail=AuditTrailMetric(
            remediation_events=1, approval_events=1, apply_events=1, verification_events=1
        ),
        cost=CostMetric(currency="USD", bedrock_cost=1.2, lambda_cost=0.3, storage_cost=0.1),
        sensitive_data_absent_verified=True,
    )


class MockDemoRunMetricsSourceTest(unittest.TestCase):
    def test_seeded_record_assembles_back_through_the_service(self) -> None:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER)
        source.register_run(_record())

        assembled = DemoRunObservabilityService(source).assemble(_admin(), deployment_id=DEPLOYMENT)

        # 조립기가 값을 지어내지 않으므로 seed한 record와 동일하게 복원된다.
        self.assertEqual(assembled.to_dict(), _record().to_dict())
        self.assertTrue(assembled.meets_gate)

    def test_unseeded_demo_run_fails_closed_through_the_service(self) -> None:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER)
        with self.assertRaises(ObservabilityAssemblyError):
            DemoRunObservabilityService(source).assemble(_admin(), deployment_id="dep-unknown")

    def test_seed_outside_source_scope_is_rejected(self) -> None:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER)
        with self.assertRaises(ObservabilitySourceScopeError):
            source.register_run(_record(customer_id="cust-other"))

    def test_reading_another_customer_scope_is_rejected(self) -> None:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER)
        source.register_run(_record())
        with self.assertRaises(ObservabilitySourceScopeError):
            source.cost(customer_id="cust-other", deployment_id=DEPLOYMENT)

    def test_duplicate_seed_is_rejected(self) -> None:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER)
        source.register_run(_record())
        with self.assertRaises(ValueError):
            source.register_run(_record())

    def test_missing_seed_raises_source_error_directly(self) -> None:
        source = MockDemoRunMetricsSource(customer_id=CUSTOMER)
        with self.assertRaises(ObservabilitySourceError):
            source.captured_at(customer_id=CUSTOMER, deployment_id="dep-unknown")


if __name__ == "__main__":
    unittest.main()
