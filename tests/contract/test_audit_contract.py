"""Audit events carry their kind in one field name across every writer."""

import unittest

from packages.contracts import AuditEventType


class AuditEventTypeContractTest(unittest.TestCase):
    def test_covers_every_audit_event_currently_written(self) -> None:
        self.assertEqual(
            {member.value for member in AuditEventType},
            {
                "DEPLOYMENT_APPROVED",
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


if __name__ == "__main__":
    unittest.main()
