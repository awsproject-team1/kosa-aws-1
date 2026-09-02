"""Audit events carry their kind in one field name across every writer."""

import unittest

from packages.contracts import AuditEventPage, AuditEventType, AuditEventView


class AuditEventTypeContractTest(unittest.TestCase):
    def test_covers_every_audit_event_currently_written(self) -> None:
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


class AuditEventViewContractTest(unittest.TestCase):
    def _view(self, **overrides: object) -> AuditEventView:
        params: dict[str, object] = {
            "event_id": "audit-001",
            "customer_id": "cust-001",
            "event_type": AuditEventType.DEPLOYMENT_REQUESTED,
            "occurred_at": "2026-09-03T00:00:00Z",
            "attributes": {"deployment_id": "dep-001", "plan_hash": "plan-001"},
        }
        params.update(overrides)
        return AuditEventView(**params)  # type: ignore[arg-type]

    def test_to_dict_merges_fixed_fields_with_domain_attributes(self) -> None:
        self.assertEqual(
            self._view().to_dict(),
            {
                "event_id": "audit-001",
                "customer_id": "cust-001",
                "event_type": "DEPLOYMENT_REQUESTED",
                "occurred_at": "2026-09-03T00:00:00Z",
                "deployment_id": "dep-001",
                "plan_hash": "plan-001",
            },
        )

    def test_internal_storage_markers_never_leak_into_the_view(self) -> None:
        view = self._view(
            attributes={
                "PK": "CUSTOMER#cust-001",
                "SK": "AUDIT#2026-09-03T00:00:00Z#audit-001",
                "entity_type": "AUDIT_EVENT",
                "version": 1,
                "GSI1PK": "x",
                "deployment_id": "dep-001",
            }
        )
        self.assertEqual(dict(view.attributes), {"deployment_id": "dep-001"})
        self.assertNotIn("PK", view.to_dict())
        self.assertNotIn("entity_type", view.to_dict())

    def test_fixed_fields_cannot_be_overridden_through_attributes(self) -> None:
        view = self._view(
            attributes={"event_type": "SPOOFED", "customer_id": "other", "deployment_id": "dep-001"}
        )
        payload = view.to_dict()
        self.assertEqual(payload["event_type"], "DEPLOYMENT_REQUESTED")
        self.assertEqual(payload["customer_id"], "cust-001")

    def test_attributes_are_immutable(self) -> None:
        view = self._view()
        with self.assertRaises(TypeError):
            view.attributes["injected"] = "x"  # type: ignore[index]

    def test_rejects_naive_or_empty_timestamps_and_ids(self) -> None:
        with self.assertRaises(ValueError):
            self._view(occurred_at="2026-09-03T00:00:00")
        with self.assertRaises(ValueError):
            self._view(event_id="  ")
        with self.assertRaises(TypeError):
            self._view(event_type="DEPLOYMENT_REQUESTED")


class AuditEventPageContractTest(unittest.TestCase):
    def _view(self) -> AuditEventView:
        return AuditEventView(
            event_id="audit-001",
            customer_id="cust-001",
            event_type=AuditEventType.DEPLOYMENT_APPROVED,
            occurred_at="2026-09-03T00:00:00Z",
            attributes={"deployment_id": "dep-001"},
        )

    def test_to_dict_omits_cursor_when_absent(self) -> None:
        page = AuditEventPage(events=(self._view(),))
        payload = page.to_dict()
        self.assertNotIn("next_cursor", payload)
        self.assertEqual(len(payload["events"]), 1)  # type: ignore[arg-type]

    def test_to_dict_includes_cursor_when_present(self) -> None:
        page = AuditEventPage(events=(self._view(),), next_cursor="cursor-abc")
        self.assertEqual(page.to_dict()["next_cursor"], "cursor-abc")

    def test_rejects_non_view_members_and_blank_cursor(self) -> None:
        with self.assertRaises(TypeError):
            AuditEventPage(events=("not-a-view",))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AuditEventPage(events=(), next_cursor="  ")


if __name__ == "__main__":
    unittest.main()
