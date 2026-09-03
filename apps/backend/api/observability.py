"""M4 A 데모 폐루프 관측·비용 기록 조립 (ADR-0021 §3, Admin 전용).

`dev → main` 릴리스 게이트는 데모 폐루프 1회 실행의 관측·비용 값을 요구한다(ADR-0021 §3).
이 서비스는 그 값을 durable한 사실에서 조립해 `DemoRunObservability`로 돌려준다. 값을 만들어
내지 않고, 주입된 read-only source가 돌려준 사실만 모은다. source가 값을 돌려주지 못하면
계약의 `meets_gate`가 그 항목을 미충족으로 표시하므로 "관측했다"의 판단이 사람마다 달라지지
않는다.

이 경계는 CloudWatch/CloudTrail/Cost Explorer를 직접 호출하지 않는다. 그 live metric adapter는
D/A의 배포 통합에서 주입되고, 이 저장소에서는 fixture/mock source로 병렬 검증한다. write 표면은
없다 — 관측·비용 기록은 조회 전용이다.
"""

from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
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


class DemoRunMetricsSource(Protocol):
    """데모 실행 하나의 관측·비용 사실을 durable 저장소에서 읽는 read-only source.

    각 메서드는 (customer_id, deployment_id) scope 안의 사실만 돌려주고, 사실이 없으면
    조립기가 fail-closed하도록 예외를 던진다(값을 지어내지 않는다).
    """

    def assessment_success(
        self, *, customer_id: str, deployment_id: str
    ) -> AssessmentSuccessMetric: ...

    def bedrock_usage(self, *, customer_id: str, deployment_id: str) -> BedrockUsageMetric: ...

    def queue_health(self, *, customer_id: str, deployment_id: str) -> QueueHealthMetric: ...

    def job_resumption(self, *, customer_id: str, deployment_id: str) -> JobResumptionMetric: ...

    def plan_apply(self, *, customer_id: str, deployment_id: str) -> PlanApplyMetric: ...

    def audit_trail(self, *, customer_id: str, deployment_id: str) -> AuditTrailMetric: ...

    def cost(self, *, customer_id: str, deployment_id: str) -> CostMetric: ...

    def sensitive_data_absent_verified(self, *, customer_id: str, deployment_id: str) -> bool: ...

    def captured_at(self, *, customer_id: str, deployment_id: str) -> str: ...


class ObservabilityAssemblyError(RuntimeError):
    """조립에 필요한 사실을 read source에서 얻지 못했을 때 던진다(fail-closed)."""


class DemoRunObservabilityService:
    """Admin이 데모 실행 하나의 관측·비용 기록을 조회한다(ADR-0021 §3).

    조회는 `READ_OBSERVABILITY`(Admin 전용)이고 principal의 customer scope 안에서만 이뤄진다.
    조립은 순수하다 — source가 돌려준 metric을 계약으로 묶기만 하고, 어떤 항목도 만들어내거나
    보정하지 않는다. 게이트 통과 여부는 계약의 `meets_gate`가 결정한다.
    """

    def __init__(self, source: DemoRunMetricsSource) -> None:
        if source is None:
            raise TypeError("source is required")
        self._source = source

    def assemble(self, principal: Principal, *, deployment_id: str) -> DemoRunObservability:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        authorize(principal, Action.READ_OBSERVABILITY)
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise ValueError("deployment_id must be a non-empty string")

        customer_id = principal.customer_id
        scope = {"customer_id": customer_id, "deployment_id": deployment_id}
        try:
            record = DemoRunObservability(
                customer_id=customer_id,
                deployment_id=deployment_id,
                captured_at=self._source.captured_at(**scope),
                assessment_success=self._source.assessment_success(**scope),
                bedrock_usage=self._source.bedrock_usage(**scope),
                queue_health=self._source.queue_health(**scope),
                job_resumption=self._source.job_resumption(**scope),
                plan_apply=self._source.plan_apply(**scope),
                audit_trail=self._source.audit_trail(**scope),
                cost=self._source.cost(**scope),
                sensitive_data_absent_verified=self._source.sensitive_data_absent_verified(**scope),
            )
        except (LookupError, TypeError, ValueError) as error:
            # source가 사실을 돌려주지 못하거나(LookupError) 계약에 맞지 않는 값을
            # 돌려주면(TypeError/ValueError) 조립이 실패한 것으로 본다. 인가는 이미
            # 위에서 끝났으므로 이 구간의 예외는 모두 fail-closed 조립 실패다.
            raise ObservabilityAssemblyError(
                "demo-run observability facts are incomplete or invalid"
            ) from error
        return record
