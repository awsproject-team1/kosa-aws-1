"""Security tests for fail-closed Cognito claims and Backend authorization."""

import unittest

from apps.backend.auth import (
    Action,
    AuthorizationDenied,
    InvalidIdentityClaims,
    Principal,
    Role,
    authorize,
)


def access_claims(*groups: object) -> dict[str, object]:
    return {
        "token_use": "access",
        "sub": "subject-001",
        "client_id": "client-001",
        "cognito:groups": list(groups),
    }


class AuthBoundarySecurityTest(unittest.TestCase):
    def test_missing_or_malformed_identity_claims_are_denied(self) -> None:
        invalid_claims: list[tuple[str, dict[str, object]]] = []
        for name in ("token_use", "sub", "client_id", "cognito:groups"):
            claims = access_claims("User")
            del claims[name]
            invalid_claims.append((f"missing {name}", claims))

        invalid_claims.extend(
            [
                ("id token", access_claims("User") | {"token_use": "id"}),
                ("non-string token_use", access_claims("User") | {"token_use": 1}),
                ("empty sub", access_claims("User") | {"sub": "  "}),
                ("non-string sub", access_claims("User") | {"sub": 1}),
                ("empty client_id", access_claims("User") | {"client_id": ""}),
                ("non-string client_id", access_claims("User") | {"client_id": []}),
                ("groups string", access_claims("User") | {"cognito:groups": "User"}),
                ("empty groups", access_claims()),
                ("non-string group", access_claims("User", 1)),
                ("empty group", access_claims("User", " ")),
                ("unknown group", access_claims("Operator")),
                ("wrong-case group", access_claims("admin")),
            ]
        )

        for name, claims in invalid_claims:
            with self.subTest(name=name):
                with self.assertRaises(InvalidIdentityClaims):
                    Principal.from_verified_claims(claims)

    def test_id_token_audience_cannot_replace_access_token_client_id(self) -> None:
        claims = access_claims("User")
        claims["token_use"] = "id"
        claims["aud"] = claims.pop("client_id")

        with self.assertRaises(InvalidIdentityClaims):
            Principal.from_verified_claims(claims)

    def test_body_style_role_fields_do_not_grant_a_product_role(self) -> None:
        claims = access_claims("ExternalGroup")
        claims.update({"role": "Admin", "roles": ["Admin"], "is_admin": True})

        with self.assertRaises(InvalidIdentityClaims):
            Principal.from_verified_claims(claims)

    def test_extra_role_fields_cannot_upgrade_a_user_principal(self) -> None:
        claims = access_claims("User")
        claims.update({"role": "Admin", "roles": ["Admin"], "is_admin": True})

        principal = Principal.from_verified_claims(claims)

        self.assertEqual(principal.roles, frozenset({Role.USER}))

    def test_untyped_or_unknown_actions_are_denied(self) -> None:
        principal = Principal.from_verified_claims(access_claims("User"))

        for action in ("START_ASSESSMENT", "DELETE_PLATFORM", None):
            with self.subTest(action=action):
                with self.assertRaises(AuthorizationDenied):
                    authorize(principal, action)  # type: ignore[arg-type]

    def test_only_normalized_principals_can_be_authorized(self) -> None:
        with self.assertRaisesRegex(TypeError, "principal must be a Principal"):
            authorize(access_claims("User"), Action.READ_JOB)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
