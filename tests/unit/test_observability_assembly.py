"""M4 A 관측·비용 조립 서비스와 감사 범주 매핑 (ADR-0021 §3)."""

import unittest

from apps.backend.api.observability import (
    DemoRunObservabilityService,
    ObservabilityAssemblyError,
)
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.repositories import assemble_audit_trail_metric
from packages.contracts import (
    AssessmentSuccessMetric,
    AuditEventType,
    AuditTrailMetric,
    BedrockUsageMetric,
    CostMetric,
    JobResumptionMetric,
    ObservabilityGateItem,
    PlanApplyMetric,
    QueueHealthMetric,
)

CUSTOMER = "cust-001"
DEPLOYMENT = "dep-001"
CAPTURED_AT = "2026-09-03T00:00:00+00:00"


def _principal(*, role: Role = Role.ADMIN, customer_id: str = CUSTOMER) -> Principal:
    return Principal(
        subject="admin-001",
        client_id="client-001",
        customer_id=customer_id,
        roles=frozenset({role}),
    )


def _passing_facts() -> dict[str, object]:
    return {
        "captured_at": CAPTURED_AT,
        "assessment_success": AssessmentSuccessMetric(planned_evaluations=18, execution_errors=0),
        "bedrock_usage": BedrockUsageMetric(
            calls_by_role={"assessment": 12},
            tokens_by_role={"assessment": 34_000},
            p95_latency_ms_by_role={"assessment": 1800.0},
        ),
        "queue_health": QueueHealthMetric(dlq_depth=0, max_queue_age_seconds=42),
        "job_resumption": JobResumptionMetric(
            checkpoint_resumptions=1, requeue_before_visibility_timeout_observed=True
        ),
        "plan_apply": PlanApplyMetric(plan_failures=0, apply_failures=0, unapproved_applies=0),
        "audit_trail": AuditTrailMetric(
            remediation_events=1, approval_events=1, apply_events=1, verification_events=1
        ),
        "cost": CostMetric(currency="USD", bedrock_cost=1.2, lambda_cost=0.3, storage_cost=0.1),
        "sensitive_data_absent_verified": True,
    }


class _FakeSource:
    """(customer_id, deployment_id) scope 안의 사실만 돌려주는 read-only fake."""

    def __init__(self, facts: dict[str, object], *, fault: str | None = None) -> None:
        self._facts = facts
        self._fault = fault
        self.scopes: list[tuple[str, str]] = []

    def _get(self, key: str, *, customer_id: str, deployment_id: str) -> object:
        self.scopes.append((customer_id, deployment_id))
        if self._fault == key:
            raise KeyError(key)
        return self._facts[key]

    def captured_at(self, *, customer_id: str, deployment_id: str) -> str:
        value = self._get("captured_at", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, str)
        return value

    def assessment_success(
        self, *, customer_id: str, deployment_id: str
    ) -> AssessmentSuccessMetric:
        value = self._get(
            "assessment_success", customer_id=customer_id, deployment_id=deployment_id
        )
        assert isinstance(value, AssessmentSuccessMetric)
        return value

    def bedrock_usage(self, *, customer_id: str, deployment_id: str) -> BedrockUsageMetric:
        value = self._get("bedrock_usage", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, BedrockUsageMetric)
        return value

    def queue_health(self, *, customer_id: str, deployment_id: str) -> QueueHealthMetric:
        value = self._get("queue_health", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, QueueHealthMetric)
        return value

    def job_resumption(self, *, customer_id: str, deployment_id: str) -> JobResumptionMetric:
        value = self._get("job_resumption", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, JobResumptionMetric)
        return value

    def plan_apply(self, *, customer_id: str, deployment_id: str) -> PlanApplyMetric:
        value = self._get("plan_apply", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, PlanApplyMetric)
        return value

    def audit_trail(self, *, customer_id: str, deployment_id: str) -> AuditTrailMetric:
        value = self._get("audit_trail", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, AuditTrailMetric)
        return value

    def cost(self, *, customer_id: str, deployment_id: str) -> CostMetric:
        value = self._get("cost", customer_id=customer_id, deployment_id=deployment_id)
        assert isinstance(value, CostMetric)
        return value

    def sensitive_data_absent_verified(self, *, customer_id: str, deployment_id: str) -> bool:
        value = self._get(
            "sensitive_data_absent_verified",
            customer_id=customer_id,
            deployment_id=deployment_id,
        )
        assert isinstance(value, bool)
        return value


class DemoRunObservabilityServiceTest(unittest.TestCase):
    def test_admin_assembles_a_passing_record_in_customer_scope(self) -> None:
        source = _FakeSource(_passing_facts())
        record = DemoRunObservabilityService(source).assemble(
            _principal(), deployment_id=DEPLOYMENT
        )
        self.assertTrue(record.meets_gate)
        self.assertEqual(record.customer_id, CUSTOMER)
        self.assertEqual(record.deployment_id, DEPLOYMENT)
        # 모든 조회가 principal의 customer scope로만 이뤄졌다.
        self.assertTrue(all(scope == (CUSTOMER, DEPLOYMENT) for scope in source.scopes))

    def test_user_cannot_read_observability(self) -> None:
        source = _FakeSource(_passing_facts())
        with self.assertRaises(AuthorizationDenied):
            DemoRunObservabilityService(source).assemble(
                _principal(role=Role.USER), deployment_id=DEPLOYMENT
            )
        # 인가 실패 시 source에 도달하지 않는다.
        self.assertEqual(source.scopes, [])

    def test_a_missing_fact_fails_closed(self) -> None:
        source = _FakeSource(_passing_facts(), fault="cost")
        with self.assertRaises(ObservabilityAssemblyError):
            DemoRunObservabilityService(source).assemble(_principal(), deployment_id=DEPLOYMENT)

    def test_unmet_item_is_carried_through_not_hidden(self) -> None:
        facts = _passing_facts()
        facts["queue_health"] = QueueHealthMetric(dlq_depth=2, max_queue_age_seconds=5)
        record = DemoRunObservabilityService(_FakeSource(facts)).assemble(
            _principal(), deployment_id=DEPLOYMENT
        )
        self.assertFalse(record.meets_gate)
        self.assertIn(ObservabilityGateItem.QUEUE_HEALTH, record.unmet_items())

    def test_blank_deployment_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DemoRunObservabilityService(_FakeSource(_passing_facts())).assemble(
                _principal(), deployment_id="  "
            )


class AssembleAuditTrailMetricTest(unittest.TestCase):
    def test_maps_event_types_to_gate_categories(self) -> None:
        metric = assemble_audit_trail_metric(
            event_counts={
                AuditEventType.REMEDIATION_DECIDED: 3,
                AuditEventType.DEPLOYMENT_APPROVED: 1,
                # 게이트 범주에 들지 않는 종류는 무시된다.
                AuditEventType.POLICY_PROFILE_PUBLISHED: 5,
            },
            apply_success_count=1,
            verification_count=1,
        )
        self.assertEqual(metric.remediation_events, 3)
        self.assertEqual(metric.approval_events, 1)
        self.assertEqual(metric.apply_events, 1)
        self.assertEqual(metric.verification_events, 1)
        self.assertTrue(metric.meets_gate)

    def test_missing_category_yields_a_failing_metric(self) -> None:
        metric = assemble_audit_trail_metric(
            event_counts={AuditEventType.REMEDIATION_DECIDED: 1},
            apply_success_count=0,
            verification_count=0,
        )
        self.assertFalse(metric.meets_gate)

    def test_negative_count_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            assemble_audit_trail_metric(
                event_counts={AuditEventType.REMEDIATION_DECIDED: -1},
                apply_success_count=0,
                verification_count=0,
            )

    def test_non_audit_event_key_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            assemble_audit_trail_metric(
                event_counts={"REMEDIATION_DECIDED": 1},  # type: ignore[dict-item]
                apply_success_count=1,
                verification_count=1,
            )


if __name__ == "__main__":
    unittest.main()
