"""Unit tests for Cognito principal normalization and action RBAC."""

import unittest

from apps.backend.auth import Action, AuthorizationDenied, Principal, Role, authorize


def access_claims(*groups: str) -> dict[str, object]:
    return {
        "token_use": "access",
        "sub": "subject-001",
        "client_id": "client-001",
        "custom:customer_id": "cust-001",
        "cognito:groups": list(groups),
    }


class AuthBoundaryTest(unittest.TestCase):
    def test_user_claims_create_an_immutable_user_principal(self) -> None:
        principal = Principal.from_verified_claims(access_claims("User"))

        self.assertEqual(principal.subject, "subject-001")
        self.assertEqual(principal.client_id, "client-001")
        self.assertEqual(principal.customer_id, "cust-001")
        self.assertEqual(principal.roles, frozenset({Role.USER}))
        with self.assertRaisesRegex(AttributeError, "cannot assign"):
            principal.subject = "another-subject"  # type: ignore[misc]

    def test_user_can_start_assessments_and_read_jobs(self) -> None:
        principal = Principal.from_verified_claims(access_claims("User"))

        self.assertIsNone(authorize(principal, Action.START_ASSESSMENT))
        self.assertIsNone(authorize(principal, Action.READ_JOB))

    def test_user_can_start_a_deployment_but_not_reject_it(self) -> None:
        # ADR-0019 §4: START_DEPLOYMENT is a User action; §8: reject is Admin only.
        principal = Principal.from_verified_claims(access_claims("User"))

        self.assertIsNone(authorize(principal, Action.START_DEPLOYMENT))
        with self.assertRaises(AuthorizationDenied):
            authorize(principal, Action.REJECT_DEPLOYMENT)
        with self.assertRaises(AuthorizationDenied):
            authorize(principal, Action.APPROVE_DEPLOYMENT)
        with self.assertRaises(AuthorizationDenied):
            authorize(principal, Action.READ_AUDIT_EVENTS)

    def test_admin_inherits_current_user_actions_and_can_approve_deployments(self) -> None:
        principal = Principal.from_verified_claims(access_claims("Admin"))

        self.assertEqual(principal.roles, frozenset({Role.ADMIN}))
        self.assertIsNone(authorize(principal, Action.START_ASSESSMENT))
        self.assertIsNone(authorize(principal, Action.READ_JOB))
        self.assertIsNone(authorize(principal, Action.START_DEPLOYMENT))
        self.assertIsNone(authorize(principal, Action.APPROVE_DEPLOYMENT))
        self.assertIsNone(authorize(principal, Action.REJECT_DEPLOYMENT))
        self.assertIsNone(authorize(principal, Action.READ_AUDIT_EVENTS))
        self.assertIsNone(authorize(principal, Action.MANAGE_REMEDIATION_EXCEPTIONS))

    def test_unknown_cognito_groups_are_ignored_when_a_product_role_remains(self) -> None:
        principal = Principal.from_verified_claims(access_claims("ExternalGroup", "User"))

        self.assertEqual(principal.roles, frozenset({Role.USER}))

    def test_customer_scope_claim_is_required(self) -> None:
        claims = access_claims("User")
        del claims["custom:customer_id"]

        with self.assertRaisesRegex(ValueError, "missing required claim: custom:customer_id"):
            Principal.from_verified_claims(claims)

    def test_action_vocabulary_is_limited_to_the_approved_api_slice(self) -> None:
        self.assertEqual(
            {action.value for action in Action},
            {
                "START_ASSESSMENT",
                "START_REMEDIATION",
                "START_DEPLOYMENT",
                "READ_JOB",
                "APPROVE_DEPLOYMENT",
                "REJECT_DEPLOYMENT",
                "READ_AUDIT_EVENTS",
                "MANAGE_REMEDIATION_EXCEPTIONS",
                "MANAGE_POLICY_SOURCES",
                "PUBLISH_POLICY_PROFILE",
            },
        )


if __name__ == "__main__":
    unittest.main()
