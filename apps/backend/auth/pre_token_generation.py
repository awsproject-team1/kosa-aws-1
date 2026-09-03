"""Cognito Pre-Token-Generation V2 trigger that injects tenant identity claims.

The API authorizer accepts the Cognito access token, and the Backend identity
boundary (`apps.backend.auth.principal`) requires a non-empty `custom:customer_id`
claim on that access token. Cognito does not place custom user-pool attributes on
access tokens by default, so this trigger copies the user's already-stored
`custom:customer_id` attribute into the access token at issue time.

It fails closed: if the attribute is absent or blank, it does not fabricate a
tenant identity. It leaves the token unmodified so the request presents a token
without `custom:customer_id`, which the Backend then rejects. This keeps the
tenant claim sourced from a stored, administrator-controlled attribute rather
than from anything a caller can influence.
"""

from __future__ import annotations

from typing import Any

_CUSTOMER_ID_ATTRIBUTE = "custom:customer_id"


def lambda_handler(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """Add the stored tenant claim to the access token for a V2 trigger event.

    The handler only augments the access token when the user has a non-empty
    `custom:customer_id` attribute. It never removes or overrides other claims
    beyond adding this single tenant claim, and it always returns the event so
    Cognito can continue issuing the token.
    """
    request = event.get("request")
    if not isinstance(request, dict):
        return event

    user_attributes = request.get("userAttributes")
    if not isinstance(user_attributes, dict):
        return event

    customer_id = user_attributes.get(_CUSTOMER_ID_ATTRIBUTE)
    if not isinstance(customer_id, str) or not customer_id.strip():
        # Fail closed: without a stored tenant attribute, do not synthesize one.
        return event

    response = event.setdefault("response", {})
    if not isinstance(response, dict):
        response = {}
        event["response"] = response

    override = response.setdefault("claimsAndScopeOverrideDetails", {})
    if not isinstance(override, dict):
        override = {}
        response["claimsAndScopeOverrideDetails"] = override

    access_generation = override.setdefault("accessTokenGeneration", {})
    if not isinstance(access_generation, dict):
        access_generation = {}
        override["accessTokenGeneration"] = access_generation

    claims_to_add = access_generation.setdefault("claimsToAddOrOverride", {})
    if not isinstance(claims_to_add, dict):
        claims_to_add = {}
        access_generation["claimsToAddOrOverride"] = claims_to_add

    claims_to_add[_CUSTOMER_ID_ATTRIBUTE] = customer_id
    return event
