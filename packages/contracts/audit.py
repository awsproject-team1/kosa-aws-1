"""Audit event vocabulary and read projection shared by every `AUDIT_EVENT` writer."""

from collections.abc import Mapping
from dataclasses import dataclass
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
    # M3 apply lifecycle (ADR-0019 §5·§7, DATABASE.md "완료 Event 경계"). These name the
    # four gate categories the M4 audit-trail metric maps onto, so they are part of the
    # vocabulary even where the writer is still an integration step.
    APPLY_DISPATCHED = "APPLY_DISPATCHED"
    APPLY_COMPLETED = "APPLY_COMPLETED"
    APPLY_FAILED = "APPLY_FAILED"
    POST_DEPLOY_VERIFIED = "POST_DEPLOY_VERIFIED"
    MANUAL_RECONCILIATION_REQUIRED = "MANUAL_RECONCILIATION_REQUIRED"


# Storage bookkeeping that is never part of the audit trail's public meaning. The read
# projection strips these so a new infrastructure attribute cannot silently become a
# published field, and so key material is never echoed back to a client.
_NON_DETAIL_ATTRIBUTES = frozenset(
    {
        "PK",
        "SK",
        "GSI1PK",
        "GSI1SK",
        "GSI2PK",
        "GSI2SK",
        "GSI3PK",
        "GSI3SK",
        "entity_type",
        "version",
        "customer_id",
        "event_id",
        "occurred_at",
        "event_type",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    """One immutable audit trail entry as returned by `GET /audit-events`.

    The four identity fields are written by every writer and are the only values the
    reader interprets. Writer-specific payload stays in `details` verbatim rather than
    being flattened into typed fields: the trail spans seven writers with different
    payloads, and a per-writer schema here would have to change on every new event kind
    while adding nothing the caller cannot read from the values themselves.
    """

    event_id: str
    event_type: AuditEventType
    occurred_at: str
    customer_id: str
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        for name in ("event_id", "customer_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType")
        # An audit entry whose time cannot be ordered is not an audit entry: the page is
        # returned newest-first, and a naive timestamp orders differently per runtime.
        require_offset_aware_timestamp(self.occurred_at, "occurred_at")
        if not isinstance(self.details, Mapping):
            raise TypeError("details must be a mapping")
        for key in self.details:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("details keys must be non-empty strings")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at,
            "customer_id": self.customer_id,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEventPage:
    """One page of the audit trail, newest first."""

    events: tuple[AuditEvent, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple):
            raise TypeError("events must be a tuple")
        for event in self.events:
            if not isinstance(event, AuditEvent):
                raise TypeError("events must contain AuditEvent values")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor.strip()
        ):
            raise ValueError("next_cursor must be a non-empty string or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "events": [event.to_dict() for event in self.events],
            "next_cursor": self.next_cursor,
        }


def audit_event_details(item: Mapping[str, object]) -> dict[str, object]:
    """Return a stored audit item's payload without its storage bookkeeping."""
    if not isinstance(item, Mapping):
        raise TypeError("item must be a mapping")
    return {
        key: value
        for key, value in item.items()
        if isinstance(key, str) and key not in _NON_DETAIL_ATTRIBUTES
    }
