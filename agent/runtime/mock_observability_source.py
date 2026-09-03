"""M4 A 관측·비용 조립의 결정적 in-memory Mock source (ADR-0021 §3).

`apps/backend/api/observability.py`가 정의한 `DemoRunMetricsSource` Protocol을 구현한다.
D가 live CloudWatch/CloudTrail/Cost Explorer 어댑터를 주입하기 전에, A/D/Shared가
Fixture/Mock으로 조립 경계와 그 배선을 병렬 검증할 수 있게 한다. 실제 관측 시스템을 호출하지
않고, seed한 값을 (customer_id, deployment_id) scope 안에서만 돌려준다. write 표면은 없다.

live 어댑터가 들어오는 자리를 그대로 흉내 낸다: 조립기는 이 source를 주입받아
`DemoRunObservability`를 만들고, 조립기가 값을 지어내지 않으므로 여기서 seed하지 않은 데모
실행을 조회하면 fail-closed로 거부된다(값 부재 = 미충족의 근거).
"""

from __future__ import annotations

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


class ObservabilitySourceError(LookupError):
    """Mock source 조회가 성립하지 않을 때 발생한다(seed 부재 등).

    `LookupError`를 상속해, 조립기(`DemoRunObservabilityService`)가 source 조회 실패를
    fail-closed 조립 실패로 감싸는 경로에 걸리게 한다 — live 어댑터의 "사실 없음"과 같은
    방식으로 처리된다(값 부재 = 미충족의 근거).
    """


class ObservabilitySourceScopeError(ObservabilitySourceError):
    """요청이 Mock에 설정된 단일 scope 밖을 대상으로 할 때 발생한다."""


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class MockDemoRunMetricsSource:
    """정확히 하나의 customer scope로 제한된 결정적 관측·비용 Mock source.

    `register_run`으로 데모 실행 하나의 `DemoRunObservability`를 seed하면, 조립기가 부르는
    항목별 메서드가 그 record에서 값을 꺼내 돌려준다. seed는 조립기가 만들 record와 같은
    (customer_id, deployment_id)여야 한다 — Mock이 계약 정합성을 미리 강제해, 조립기가 서로
    다른 scope의 값을 섞는 상황을 테스트에서 재현할 수 없게 한다.
    """

    def __init__(self, *, customer_id: str) -> None:
        self._customer_id = _require_non_empty(customer_id, "customer_id")
        self._runs: dict[str, DemoRunObservability] = {}

    def register_run(self, record: DemoRunObservability) -> None:
        """결정적 조회를 위해 데모 실행의 관측·비용 record를 등록한다(테스트 seed)."""
        if not isinstance(record, DemoRunObservability):
            raise TypeError("record must be a DemoRunObservability")
        if record.customer_id != self._customer_id:
            raise ObservabilitySourceScopeError("record customer_id is outside the source scope")
        if record.deployment_id in self._runs:
            raise ValueError(f"duplicate demo run: {record.deployment_id}")
        self._runs[record.deployment_id] = record

    def _record(self, *, customer_id: str, deployment_id: str) -> DemoRunObservability:
        if customer_id != self._customer_id:
            raise ObservabilitySourceScopeError("customer_id is outside the source scope")
        _require_non_empty(deployment_id, "deployment_id")
        record = self._runs.get(deployment_id)
        if record is None:
            # seed 부재는 흐름이 성립하지 않는 오류다. 조립기가 이를 fail-closed 조립 실패로
            # 감싸므로 "값 없으면 미충족"이 유지된다.
            raise ObservabilitySourceError(
                f"no observability record registered for demo run {deployment_id}"
            )
        return record

    def captured_at(self, *, customer_id: str, deployment_id: str) -> str:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).captured_at

    def assessment_success(
        self, *, customer_id: str, deployment_id: str
    ) -> AssessmentSuccessMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).assessment_success

    def bedrock_usage(self, *, customer_id: str, deployment_id: str) -> BedrockUsageMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).bedrock_usage

    def queue_health(self, *, customer_id: str, deployment_id: str) -> QueueHealthMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).queue_health

    def job_resumption(self, *, customer_id: str, deployment_id: str) -> JobResumptionMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).job_resumption

    def plan_apply(self, *, customer_id: str, deployment_id: str) -> PlanApplyMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).plan_apply

    def audit_trail(self, *, customer_id: str, deployment_id: str) -> AuditTrailMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).audit_trail

    def cost(self, *, customer_id: str, deployment_id: str) -> CostMetric:
        return self._record(customer_id=customer_id, deployment_id=deployment_id).cost

    def sensitive_data_absent_verified(self, *, customer_id: str, deployment_id: str) -> bool:
        return self._record(
            customer_id=customer_id, deployment_id=deployment_id
        ).sensitive_data_absent_verified
