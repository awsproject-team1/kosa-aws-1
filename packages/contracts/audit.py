"""Audit event vocabulary shared by every writer of an `AUDIT_EVENT` item."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from packages.contracts._validation import (
    require_non_empty_string,
    require_offset_aware_timestamp,
)


class AuditEventType(StrEnum):
    """The kind of an audit event, persisted as the `event_type` attribute.

    `action` is reserved for domain payload: a `REMEDIATION_DECIDED` item already
    carries a `RemediationAction` under that name, so reusing `action` for the kind
    would make two different meanings compete for one attribute. Uniform retrieval
    (`GET /audit-events`) depends on every writer using this one field name.
    """

    DEPLOYMENT_REQUESTED = "DEPLOYMENT_REQUESTED"
    DEPLOYMENT_APPROVED = "DEPLOYMENT_APPROVED"
    DEPLOYMENT_REJECTED = "DEPLOYMENT_REJECTED"
    POLICY_SOURCE_APPROVED = "POLICY_SOURCE_APPROVED"
    POLICY_PROFILE_PUBLISHED = "POLICY_PROFILE_PUBLISHED"
    REMEDIATION_DECIDED = "REMEDIATION_DECIDED"
    REMEDIATION_EXCEPTION_APPROVED = "REMEDIATION_EXCEPTION_APPROVED"


# 조회 응답에서 감출 DynamoDB 내부 속성. 저장 항목의 key/entity 표식은 감사 payload가 아니므로
# view로 새어 나가면 안 된다. writer별 도메인 필드(deployment_id, plan_hash, reason 등)는
# `attributes`로 그대로 전달한다.
_INTERNAL_AUDIT_ATTRIBUTES = frozenset(
    {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "entity_type", "version"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEventView:
    """조회 전용 감사 이력 항목(immutable).

    모든 writer가 공유하는 고정 필드(`event_type`/`occurred_at`/`event_id`/`customer_id`)만
    타입으로 못 박고, writer마다 다른 도메인 필드는 `attributes` 맵으로 그대로 노출한다.
    이렇게 하면 새 `AuditEventType`이 추가돼도 view 타입을 바꾸지 않고 균일하게 조회된다.
    """

    event_id: str
    customer_id: str
    event_type: AuditEventType
    occurred_at: str
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_non_empty_string(self.event_id, "event_id")
        require_non_empty_string(self.customer_id, "customer_id")
        if not isinstance(self.event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType")
        require_offset_aware_timestamp(self.occurred_at, "occurred_at")
        if not isinstance(self.attributes, Mapping):
            raise TypeError("attributes must be a mapping")
        # 내부 저장 표식과 고정 필드는 attributes에서 제외해 view가 payload만 담게 한다.
        reserved = _INTERNAL_AUDIT_ATTRIBUTES | {
            "event_id",
            "customer_id",
            "event_type",
            "occurred_at",
        }
        cleaned: dict[str, object] = {}
        for key, value in self.attributes.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("attribute keys must be non-empty strings")
            if key in reserved:
                continue
            cleaned[key] = value
        object.__setattr__(self, "attributes", MappingProxyType(cleaned))

    def to_dict(self) -> dict[str, object]:
        """조회 응답 wire shape을 반환한다."""
        payload: dict[str, object] = {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at,
        }
        payload.update(self.attributes)
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEventPage:
    """감사 이력 조회 한 페이지와 다음 페이지 cursor(immutable)."""

    events: tuple[AuditEventView, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple):
            raise TypeError("events must be a tuple")
        for event in self.events:
            if not isinstance(event, AuditEventView):
                raise TypeError("events must contain AuditEventView values")
        if self.next_cursor is not None:
            require_non_empty_string(self.next_cursor, "next_cursor")

    def to_dict(self) -> dict[str, object]:
        """조회 응답 wire shape을 반환한다."""
        payload: dict[str, object] = {"events": [event.to_dict() for event in self.events]}
        if self.next_cursor is not None:
            payload["next_cursor"] = self.next_cursor
        return payload
