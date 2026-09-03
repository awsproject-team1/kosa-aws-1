"""Tests for the Cognito Pre-Token-Generation V2 tenant-claim trigger."""

import unittest

from apps.backend.auth.pre_token_generation import lambda_handler


def _v2_event(user_attributes: dict[str, object]) -> dict[str, object]:
    return {
        "triggerSource": "TokenGeneration_HostedAuth",
        "request": {"userAttributes": user_attributes, "groupConfiguration": {}},
        "response": {},
    }


def _access_claims(event: dict[str, object]) -> dict[str, object]:
    response = event.get("response", {})
    override = (
        response.get("claimsAndScopeOverrideDetails", {}) if isinstance(response, dict) else {}
    )
    access = override.get("accessTokenGeneration", {}) if isinstance(override, dict) else {}
    return access.get("claimsToAddOrOverride", {}) if isinstance(access, dict) else {}


class PreTokenGenerationTest(unittest.TestCase):
    def test_injects_stored_customer_id_into_access_token(self) -> None:
        event = _v2_event({"custom:customer_id": "kosa-sandbox", "email": "a@example.com"})

        result = lambda_handler(event)

        self.assertEqual(_access_claims(result)["custom:customer_id"], "kosa-sandbox")

    def test_missing_customer_id_leaves_token_unmodified(self) -> None:
        event = _v2_event({"email": "a@example.com"})

        result = lambda_handler(event)

        self.assertNotIn("custom:customer_id", _access_claims(result))

    def test_blank_customer_id_does_not_add_claim(self) -> None:
        event = _v2_event({"custom:customer_id": "   "})

        result = lambda_handler(event)

        self.assertNotIn("custom:customer_id", _access_claims(result))

    def test_malformed_event_is_returned_unchanged(self) -> None:
        for event in ({}, {"request": None}, {"request": {"userAttributes": None}}):
            with self.subTest(event=event):
                self.assertEqual(lambda_handler(dict(event)), event)

    def test_does_not_override_other_existing_claims(self) -> None:
        event = _v2_event({"custom:customer_id": "cust-1"})
        event["response"] = {
            "claimsAndScopeOverrideDetails": {
                "accessTokenGeneration": {"claimsToAddOrOverride": {"existing": "keep"}}
            }
        }

        result = lambda_handler(event)

        claims = _access_claims(result)
        self.assertEqual(claims["existing"], "keep")
        self.assertEqual(claims["custom:customer_id"], "cust-1")


if __name__ == "__main__":
    unittest.main()
