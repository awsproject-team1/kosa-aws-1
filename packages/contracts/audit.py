"""Audit event vocabulary shared by every writer of an `AUDIT_EVENT` item."""

from enum import StrEnum


class AuditEventType(StrEnum):
    """The kind of an audit event, persisted as the `event_type` attribute.

    `action` is reserved for domain payload: a `REMEDIATION_DECIDED` item already
    carries a `RemediationAction` under that name, so reusing `action` for the kind
    would make two different meanings compete for one attribute. Uniform retrieval
    (`GET /audit-events`) depends on every writer using this one field name.
    """

    DEPLOYMENT_APPROVED = "DEPLOYMENT_APPROVED"
    POLICY_SOURCE_APPROVED = "POLICY_SOURCE_APPROVED"
    POLICY_PROFILE_PUBLISHED = "POLICY_PROFILE_PUBLISHED"
    REMEDIATION_DECIDED = "REMEDIATION_DECIDED"
    REMEDIATION_EXCEPTION_APPROVED = "REMEDIATION_EXCEPTION_APPROVED"
