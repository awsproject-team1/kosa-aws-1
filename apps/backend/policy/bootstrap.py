"""Idempotently publish an approved Rule Registry to one customer catalog.

This is an operator/deployment boundary, not a customer-policy upload feature.
It only publishes the reviewed, committed MVP Registry.  A differing item at an
existing immutable key fails closed rather than changing a policy already used
by an Assessment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from apps.backend.policy.registry import PolicyRegistry


class PolicyCatalogBootstrapError(RuntimeError):
    """The customer Policy Catalog could not be published safely."""


class DynamoTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_item(self, **kwargs: object) -> object: ...


class DynamoDbPolicyCatalogBootstrap:
    """Publish Registry definitions to one DynamoDB customer partition."""

    def __init__(self, table: DynamoTable, *, customer_id: str) -> None:
        if table is None:
            raise TypeError("table is required")
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        self._table = table
        self._customer_id = customer_id

    def publish(self, registry: PolicyRegistry) -> int:
        if not isinstance(registry, PolicyRegistry):
            raise TypeError("registry must be a PolicyRegistry")
        published = 0
        for item in _items_for_registry(registry, self._customer_id):
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
                )
                published += 1
            except Exception as error:
                if _provider_error_code(error) != "ConditionalCheckFailedException":
                    raise PolicyCatalogBootstrapError("policy catalog publish failed") from None
                if not self._matches_existing(item):
                    raise PolicyCatalogBootstrapError(
                        "policy catalog key already contains different immutable content"
                    ) from None
        return published

    def _matches_existing(self, expected: dict[str, object]) -> bool:
        try:
            existing = self._table.get_item(
                Key={"PK": expected["PK"], "SK": expected["SK"]}, ConsistentRead=True
            ).get("Item")
        except Exception:
            raise PolicyCatalogBootstrapError("policy catalog read after conflict failed") from None
        return isinstance(existing, Mapping) and dict(existing) == expected


def _items_for_registry(
    registry: PolicyRegistry, customer_id: str
) -> tuple[dict[str, object], ...]:
    pk = f"CUSTOMER#{customer_id}"
    items: list[dict[str, object]] = []
    for source in registry.sources:
        items.append(
            {
                "PK": pk,
                "SK": f"POLICY_SOURCE#{source.source_id}#VERSION#{source.version}",
                "entity_type": "POLICY_SOURCE",
                "customer_id": customer_id,
                **source.to_dict(),
            }
        )
    for rule in registry.rules:
        items.append(
            {
                "PK": pk,
                "SK": f"RULE#{rule.rule_id}#VERSION#{rule.version}",
                "entity_type": "POLICY_RULE",
                "customer_id": customer_id,
                **rule.to_dict(),
            }
        )
    for profile in registry.profiles:
        items.append(
            {
                "PK": pk,
                "SK": f"POLICY_PROFILE#{profile.policy_profile_id}",
                "entity_type": "POLICY_PROFILE",
                "customer_id": customer_id,
                **profile.to_dict(),
            }
        )
    return tuple(items)


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, Mapping) else None
    code = details.get("Code") if isinstance(details, Mapping) else None
    return code if isinstance(code, str) else None
