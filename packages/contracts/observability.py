"""Demo closed-loop observability and cost record contracts (ADR-0021 §3).

M4 릴리스 게이트는 데모 폐루프 1회 실행에 대한 관측·비용 값을 요구한다. 표의 각 항목은
"값이 비어 있으면 미충족"이라는 판정을 받는다(ADR-0021 §3). 이 계약은 그 7개 항목을
immutable하게 묶고, 값의 존재 여부로 게이트 통과를 결정하는 순수 함수를 제공한다.

이 계약은 실제 CloudWatch/CloudTrail/Cost Explorer를 호출하지 않는다. durable한 사실
(감사 이력, Job checkpoint, plan/apply run 사실)에서 값을 조립하는 A의 read 경계가 채우는
표시·검증 전용 형태다. 조립기와 live metric adapter는 다른 조각이다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from packages.contracts._validation import (
    require_non_empty_string,
    require_offset_aware_timestamp,
)


class ObservabilityGateItem(StrEnum):
    """The seven demo-run gate items whose values must all be present (ADR-0021 §3)."""

    ASSESSMENT_SUCCESS = "ASSESSMENT_SUCCESS"
    BEDROCK_USAGE = "BEDROCK_USAGE"
    QUEUE_HEALTH = "QUEUE_HEALTH"
    JOB_RESUMPTION = "JOB_RESUMPTION"
    PLAN_APPLY = "PLAN_APPLY"
    AUDIT_TRAIL = "AUDIT_TRAIL"
    COST = "COST"


def _require_non_negative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be zero or greater")


def _require_non_negative_number(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    # NaN은 자기 자신과도 같지 않으므로 이 비교로 걸러진다.
    if not value >= 0:
        raise ValueError(f"{field_name} must be a non-negative, finite number")
    if value == float("inf"):
        raise ValueError(f"{field_name} must be a non-negative, finite number")


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentSuccessMetric:
    """계획된 평가 대비 실행 오류. 게이트는 `EXECUTION_ERROR` 0건을 요구한다(ADR-0021 §3)."""

    planned_evaluations: int
    execution_errors: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.planned_evaluations, "planned_evaluations")
        _require_non_negative_int(self.execution_errors, "execution_errors")
        if self.execution_errors > self.planned_evaluations:
            raise ValueError("execution_errors cannot exceed planned_evaluations")

    @property
    def meets_gate(self) -> bool:
        """계획된 평가가 있고 실행 오류가 하나도 없으면 통과다."""
        return self.planned_evaluations > 0 and self.execution_errors == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "planned_evaluations": self.planned_evaluations,
            "execution_errors": self.execution_errors,
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BedrockUsageMetric:
    """역할별 Bedrock 호출 수·토큰·p95 지연 기록(ADR-0021 §3).

    최소 한 역할의 호출이 기록돼야 데모가 실제로 AI 평가를 돌렸다고 볼 수 있다.
    각 역할 항목은 호출 수, 토큰 합계, p95 지연(ms)을 함께 남긴다.
    """

    calls_by_role: Mapping[str, int]
    tokens_by_role: Mapping[str, int]
    p95_latency_ms_by_role: Mapping[str, float]

    def __post_init__(self) -> None:
        for name in ("calls_by_role", "tokens_by_role", "p95_latency_ms_by_role"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
        roles = set(self.calls_by_role)
        if roles != set(self.tokens_by_role) or roles != set(self.p95_latency_ms_by_role):
            raise ValueError("Bedrock usage roles must match across calls, tokens, and latency")
        cleaned_calls: dict[str, int] = {}
        cleaned_tokens: dict[str, int] = {}
        cleaned_latency: dict[str, float] = {}
        for role in roles:
            require_non_empty_string(role, "Bedrock usage role")
            _require_non_negative_int(self.calls_by_role[role], f"calls_by_role[{role}]")
            _require_non_negative_int(self.tokens_by_role[role], f"tokens_by_role[{role}]")
            _require_non_negative_number(
                self.p95_latency_ms_by_role[role], f"p95_latency_ms_by_role[{role}]"
            )
            cleaned_calls[role] = self.calls_by_role[role]
            cleaned_tokens[role] = self.tokens_by_role[role]
            cleaned_latency[role] = float(self.p95_latency_ms_by_role[role])
        object.__setattr__(self, "calls_by_role", MappingProxyType(cleaned_calls))
        object.__setattr__(self, "tokens_by_role", MappingProxyType(cleaned_tokens))
        object.__setattr__(self, "p95_latency_ms_by_role", MappingProxyType(cleaned_latency))

    @property
    def total_calls(self) -> int:
        return sum(self.calls_by_role.values())

    @property
    def meets_gate(self) -> bool:
        """역할별 호출이 최소 하나 기록됐으면 통과다."""
        return self.total_calls > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "calls_by_role": dict(self.calls_by_role),
            "tokens_by_role": dict(self.tokens_by_role),
            "p95_latency_ms_by_role": dict(self.p95_latency_ms_by_role),
            "total_calls": self.total_calls,
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueHealthMetric:
    """Queue 건전성. 게이트는 DLQ depth 0을 요구하고 queue age 최대값을 기록한다(ADR-0021 §3)."""

    dlq_depth: int
    max_queue_age_seconds: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.dlq_depth, "dlq_depth")
        _require_non_negative_int(self.max_queue_age_seconds, "max_queue_age_seconds")

    @property
    def meets_gate(self) -> bool:
        """DLQ가 비어 있으면 통과다. queue age는 기록만 하고 차단하지 않는다."""
        return self.dlq_depth == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "dlq_depth": self.dlq_depth,
            "max_queue_age_seconds": self.max_queue_age_seconds,
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class JobResumptionMetric:
    """Job 재개. checkpoint 재개 횟수와 3분 전 재큐잉 동작을 기록한다(ADR-0021 §3).

    재개는 발생하지 않을 수도 있으므로(오류 없이 완주) 존재 자체가 기록의 대상이지
    양수여야 하는 것은 아니다. 관측이 실제로 이뤄졌음을 표시하는 값이다.
    """

    checkpoint_resumptions: int
    requeue_before_visibility_timeout_observed: bool

    def __post_init__(self) -> None:
        _require_non_negative_int(self.checkpoint_resumptions, "checkpoint_resumptions")
        if not isinstance(self.requeue_before_visibility_timeout_observed, bool):
            raise TypeError("requeue_before_visibility_timeout_observed must be a bool")

    @property
    def meets_gate(self) -> bool:
        """이 항목은 값이 기록되면 충족이다(존재 기반, ADR-0021 §3). 항상 True."""
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_resumptions": self.checkpoint_resumptions,
            "requeue_before_visibility_timeout_observed": (
                self.requeue_before_visibility_timeout_observed
            ),
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanApplyMetric:
    """plan/apply 건전성. 게이트는 실패 0건과 승인 없는 apply 0건을 요구한다(ADR-0021 §3)."""

    plan_failures: int
    apply_failures: int
    unapproved_applies: int

    def __post_init__(self) -> None:
        for name in ("plan_failures", "apply_failures", "unapproved_applies"):
            _require_non_negative_int(getattr(self, name), name)

    @property
    def meets_gate(self) -> bool:
        """plan/apply 실패가 없고 승인 없는 apply가 한 건도 없으면 통과다."""
        return self.plan_failures == 0 and self.apply_failures == 0 and self.unapproved_applies == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_failures": self.plan_failures,
            "apply_failures": self.apply_failures,
            "unapproved_applies": self.unapproved_applies,
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditTrailMetric:
    """감사 이력. Remediation·Approval·Apply·Verification event가 모두 존재해야 한다(ADR-0021 §3)."""

    remediation_events: int
    approval_events: int
    apply_events: int
    verification_events: int

    def __post_init__(self) -> None:
        for name in (
            "remediation_events",
            "approval_events",
            "apply_events",
            "verification_events",
        ):
            _require_non_negative_int(getattr(self, name), name)

    @property
    def meets_gate(self) -> bool:
        """네 종류의 audit event가 모두 최소 한 건씩 있으면 통과다."""
        return (
            self.remediation_events > 0
            and self.approval_events > 0
            and self.apply_events > 0
            and self.verification_events > 0
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "remediation_events": self.remediation_events,
            "approval_events": self.approval_events,
            "apply_events": self.apply_events,
            "verification_events": self.verification_events,
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CostMetric:
    """데모 1회의 Bedrock·Lambda·저장소 비용 합계(ADR-0021 §3).

    절대 상한은 두지 않는다. 최초 실행값을 기준선으로 남기고 이후 회귀를 비교하는 값이다.
    따라서 게이트는 세 값이 모두 기록됐는지(존재)만 본다.
    """

    currency: str
    bedrock_cost: float
    lambda_cost: float
    storage_cost: float

    def __post_init__(self) -> None:
        require_non_empty_string(self.currency, "currency")
        for name in ("bedrock_cost", "lambda_cost", "storage_cost"):
            _require_non_negative_number(getattr(self, name), name)

    @property
    def total_cost(self) -> float:
        return self.bedrock_cost + self.lambda_cost + self.storage_cost

    @property
    def meets_gate(self) -> bool:
        """세 비용 값이 존재하면 충족이다(존재 기반, 상한 없음, ADR-0021 §3). 항상 True."""
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "bedrock_cost": self.bedrock_cost,
            "lambda_cost": self.lambda_cost,
            "storage_cost": self.storage_cost,
            "total_cost": self.total_cost,
            "meets_gate": self.meets_gate,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoRunObservability:
    """데모 폐루프 1회 실행의 관측·비용 기록 전체(ADR-0021 §3).

    일곱 항목이 모두 채워져야 릴리스 게이트의 관측·비용 조건을 충족한다. 각 항목은
    자신의 `meets_gate` 판정을 갖고, `unmet_items()`가 미충족 항목을 열거한다. 민감 원문이
    로그에 없음(ADR-0021 §3 말미)은 `sensitive_data_absent_verified` 플래그로 확인한다.
    """

    customer_id: str
    deployment_id: str
    captured_at: str
    assessment_success: AssessmentSuccessMetric
    bedrock_usage: BedrockUsageMetric
    queue_health: QueueHealthMetric
    job_resumption: JobResumptionMetric
    plan_apply: PlanApplyMetric
    audit_trail: AuditTrailMetric
    cost: CostMetric
    sensitive_data_absent_verified: bool = False

    def __post_init__(self) -> None:
        require_non_empty_string(self.customer_id, "customer_id")
        require_non_empty_string(self.deployment_id, "deployment_id")
        require_offset_aware_timestamp(self.captured_at, "captured_at")
        typed = {
            "assessment_success": AssessmentSuccessMetric,
            "bedrock_usage": BedrockUsageMetric,
            "queue_health": QueueHealthMetric,
            "job_resumption": JobResumptionMetric,
            "plan_apply": PlanApplyMetric,
            "audit_trail": AuditTrailMetric,
            "cost": CostMetric,
        }
        for name, expected in typed.items():
            if not isinstance(getattr(self, name), expected):
                raise TypeError(f"{name} must be a {expected.__name__}")
        if not isinstance(self.sensitive_data_absent_verified, bool):
            raise TypeError("sensitive_data_absent_verified must be a bool")

    def _gate_by_item(self) -> dict[ObservabilityGateItem, bool]:
        return {
            ObservabilityGateItem.ASSESSMENT_SUCCESS: self.assessment_success.meets_gate,
            ObservabilityGateItem.BEDROCK_USAGE: self.bedrock_usage.meets_gate,
            ObservabilityGateItem.QUEUE_HEALTH: self.queue_health.meets_gate,
            ObservabilityGateItem.JOB_RESUMPTION: self.job_resumption.meets_gate,
            ObservabilityGateItem.PLAN_APPLY: self.plan_apply.meets_gate,
            ObservabilityGateItem.AUDIT_TRAIL: self.audit_trail.meets_gate,
            ObservabilityGateItem.COST: self.cost.meets_gate,
        }

    def unmet_items(self) -> tuple[ObservabilityGateItem, ...]:
        """게이트를 충족하지 못한 항목을 정의 순서대로 돌려준다."""
        return tuple(item for item, met in self._gate_by_item().items() if not met)

    @property
    def meets_gate(self) -> bool:
        """일곱 항목이 모두 충족되고 민감 원문 부재가 확인됐으면 통과다."""
        return not self.unmet_items() and self.sensitive_data_absent_verified

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "deployment_id": self.deployment_id,
            "captured_at": self.captured_at,
            "assessment_success": self.assessment_success.to_dict(),
            "bedrock_usage": self.bedrock_usage.to_dict(),
            "queue_health": self.queue_health.to_dict(),
            "job_resumption": self.job_resumption.to_dict(),
            "plan_apply": self.plan_apply.to_dict(),
            "audit_trail": self.audit_trail.to_dict(),
            "cost": self.cost.to_dict(),
            "sensitive_data_absent_verified": self.sensitive_data_absent_verified,
            "meets_gate": self.meets_gate,
            "unmet_items": [item.value for item in self.unmet_items()],
        }
