"""Audit events carry their kind in one field name across every writer."""

import unittest

from packages.contracts import (
    AuditEvent,
    AuditEventPage,
    AuditEventType,
    audit_event_details,
)


class AuditEventTypeContractTest(unittest.TestCase):
    def test_covers_every_audit_event_in_the_agreed_vocabulary(self) -> None:
        """일곱 writer의 현재 값과 ADR-0019가 M3에서 더하기로 한 다섯 값이 전부다."""
        self.assertEqual(
            {member.value for member in AuditEventType},
            {
                "DEPLOYMENT_REQUESTED",
                "DEPLOYMENT_APPROVED",
                "DEPLOYMENT_REJECTED",
                "POLICY_SOURCE_APPROVED",
                "POLICY_PROFILE_PUBLISHED",
                "REMEDIATION_DECIDED",
                "REMEDIATION_EXCEPTION_APPROVED",
                "APPLY_DISPATCHED",
                "APPLY_COMPLETED",
                "APPLY_FAILED",
                "POST_DEPLOY_VERIFIED",
                "MANUAL_RECONCILIATION_REQUIRED",
            },
        )

    def test_is_a_string_enum_so_a_stored_attribute_round_trips(self) -> None:
        self.assertEqual(AuditEventType("DEPLOYMENT_APPROVED"), AuditEventType.DEPLOYMENT_APPROVED)
        self.assertEqual(AuditEventType.DEPLOYMENT_APPROVED.value, "DEPLOYMENT_APPROVED")

    def test_does_not_collide_with_the_remediation_action_vocabulary(self) -> None:
        """`action` stays domain payload; a REMEDIATION_DECIDED item uses both fields."""
        from packages.contracts import RemediationAction

        self.assertFalse(
            {member.value for member in AuditEventType}
            & {member.value for member in RemediationAction}
        )


class AuditEventReadContractTest(unittest.TestCase):
    """`GET /audit-events`가 돌려주는 wire shape (M2 A)."""

    def _event(self, **overrides: object) -> AuditEvent:
        values: dict[str, object] = {
            "event_id": "audit-001",
            "event_type": AuditEventType.DEPLOYMENT_APPROVED,
            "occurred_at": "2026-09-02T10:00:00Z",
            "customer_id": "cust-001",
            "details": {"deployment_id": "dep-001"},
        }
        values.update(overrides)
        return AuditEvent(**values)  # type: ignore[arg-type]

    def test_serializes_identity_and_payload(self) -> None:
        self.assertEqual(
            self._event().to_dict(),
            {
                "event_id": "audit-001",
                "event_type": "DEPLOYMENT_APPROVED",
                "occurred_at": "2026-09-02T10:00:00Z",
                "customer_id": "cust-001",
                "details": {"deployment_id": "dep-001"},
            },
        )

    def test_details_are_not_mutable_through_the_returned_value(self) -> None:
        details = {"deployment_id": "dep-001"}
        event = self._event(details=details)
        details["deployment_id"] = "dep-002"
        self.assertEqual(event.details["deployment_id"], "dep-001")
        with self.assertRaises(TypeError):
            event.details["deployment_id"] = "dep-003"  # type: ignore[index]

    def test_requires_an_orderable_timestamp(self) -> None:
        """페이지가 최신순이므로 offset 없는 시각은 순서를 정할 수 없다."""
        with self.assertRaises(ValueError):
            self._event(occurred_at="2026-09-02T10:00:00")

    def test_rejects_an_unknown_event_kind(self) -> None:
        with self.assertRaises(TypeError):
            self._event(event_type="DEPLOYMENT_APPROVED")

    def test_page_serializes_events_and_cursor(self) -> None:
        page = AuditEventPage(events=(self._event(),), next_cursor="cursor-1")
        body = page.to_dict()
        self.assertEqual(body["next_cursor"], "cursor-1")
        self.assertEqual(body["events"][0]["event_id"], "audit-001")
        self.assertIsNone(AuditEventPage(events=()).to_dict()["next_cursor"])

    def test_details_strip_storage_bookkeeping(self) -> None:
        stored = {
            "PK": "CUSTOMER#cust-001",
            "SK": "AUDIT#2026-09-02T10:00:00Z#audit-001",
            "GSI2PK": "OUTBOX#PENDING",
            "entity_type": "AUDIT_EVENT",
            "version": 1,
            "customer_id": "cust-001",
            "event_id": "audit-001",
            "occurred_at": "2026-09-02T10:00:00Z",
            "event_type": "DEPLOYMENT_APPROVED",
            "deployment_id": "dep-001",
            "approved_by": "subject-001",
        }
        self.assertEqual(
            audit_event_details(stored),
            {"deployment_id": "dep-001", "approved_by": "subject-001"},
        )


if __name__ == "__main__":
    unittest.main()
