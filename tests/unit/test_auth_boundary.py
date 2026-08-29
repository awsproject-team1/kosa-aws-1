"""Unit tests for Cognito principal normalization and action RBAC."""

import unittest

from apps.backend.auth import Action, Principal, Role, authorize


def access_claims(*groups: str) -> dict[str, object]:
    return {
        "token_use": "access",
        "sub": "subject-001",
        "client_id": "client-001",
        "cognito:groups": list(groups),
    }


class AuthBoundaryTest(unittest.TestCase):
    def test_user_claims_create_an_immutable_user_principal(self) -> None:
        principal = Principal.from_verified_claims(access_claims("User"))

        self.assertEqual(principal.subject, "subject-001")
        self.assertEqual(principal.client_id, "client-001")
        self.assertEqual(principal.roles, frozenset({Role.USER}))
        with self.assertRaisesRegex(AttributeError, "cannot assign"):
            principal.subject = "another-subject"  # type: ignore[misc]

    def test_user_can_start_assessments_and_read_jobs(self) -> None:
        principal = Principal.from_verified_claims(access_claims("User"))

        self.assertIsNone(authorize(principal, Action.START_ASSESSMENT))
        self.assertIsNone(authorize(principal, Action.READ_JOB))

    def test_admin_inherits_current_user_actions(self) -> None:
        principal = Principal.from_verified_claims(access_claims("Admin"))

        self.assertEqual(principal.roles, frozenset({Role.ADMIN}))
        self.assertIsNone(authorize(principal, Action.START_ASSESSMENT))
        self.assertIsNone(authorize(principal, Action.READ_JOB))

    def test_unknown_cognito_groups_are_ignored_when_a_product_role_remains(self) -> None:
        principal = Principal.from_verified_claims(access_claims("ExternalGroup", "User"))

        self.assertEqual(principal.roles, frozenset({Role.USER}))

    def test_action_vocabulary_is_limited_to_the_approved_api_slice(self) -> None:
        self.assertEqual(
            {action.value for action in Action},
            {"START_ASSESSMENT", "READ_JOB"},
        )


if __name__ == "__main__":
    unittest.main()
