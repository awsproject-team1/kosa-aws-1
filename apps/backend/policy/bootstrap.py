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
from packages.contracts import RuleLifecycle


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
                existing = self._read_existing(item)
                if self._matches_existing(item, existing):
                    continue
                if _is_profile_pointer(item) and self._move_profile_pointer(item, existing):
                    published += 1
                    continue
                raise PolicyCatalogBootstrapError(
                    "policy catalog key already contains different immutable content"
                ) from None
        return published

    def _move_profile_pointer(
        self, expected: dict[str, object], existing: Mapping[str, object] | None
    ) -> bool:
        """Advance a Profile's current pointer to the version this registry declares.

        Profile **판본** item은 불변이다 — 게시된 `v1`은 그대로 남는다. 그러나 current pointer는
        정의상 움직이는 것이고, Registry가 새 판본(`v2`)을 선언하면 pointer가 그것을 가리켜야 새
        Assessment와 `load_baseline`이 새 판본을 본다. `record_profile`이 고객 게시에서 하는 것과
        같은 조건부 이동이다: 지금 가리키는 판본이 읽은 값과 같을 때만 옮긴다 — 동시에 두 운영자가
        게시해도 나중 것이 앞의 것을 조용히 덮어쓰지 않는다.

        같은 판본을 가리키는데 내용이 다르면 옮기지 않는다. 그것은 "새 판본"이 아니라 "게시된 판본의
        다른 내용"이고, 그 경우는 불변 key 충돌로 fail-closed해야 한다.
        """
        if not isinstance(existing, Mapping):
            return False
        current = existing.get("current_version")
        target = expected.get("current_version")
        if not isinstance(current, str) or not isinstance(target, str) or current == target:
            return False
        try:
            self._table.put_item(
                Item=expected,
                ConditionExpression="current_version = :current",
                ExpressionAttributeValues={":current": current},
            )
        except Exception as error:
            if _provider_error_code(error) == "ConditionalCheckFailedException":
                raise PolicyCatalogBootstrapError(
                    "policy profile pointer moved concurrently; re-run the publish"
                ) from None
            raise PolicyCatalogBootstrapError("policy catalog publish failed") from None
        return True

    def _read_existing(self, expected: dict[str, object]) -> Mapping[str, object] | None:
        try:
            existing = self._table.get_item(
                Key={"PK": expected["PK"], "SK": expected["SK"]}, ConsistentRead=True
            ).get("Item")
        except Exception:
            raise PolicyCatalogBootstrapError("policy catalog read after conflict failed") from None
        return existing if isinstance(existing, Mapping) else None

    @staticmethod
    def _matches_existing(
        expected: dict[str, object], existing: Mapping[str, object] | None
    ) -> bool:
        if existing is None:
            return False
        actual = dict(existing)
        # Published registry items predate the persisted lifecycle field.  Treat
        # that one legacy shape as the approved publication it represents, while
        # still failing closed for every other content difference or lifecycle.
        if "lifecycle" not in actual and expected.get("lifecycle") == RuleLifecycle.APPROVED.value:
            actual["lifecycle"] = RuleLifecycle.APPROVED.value
        return actual == expected


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
                "lifecycle": RuleLifecycle.APPROVED.value,
                **rule.to_dict(),
            }
        )
    for profile in registry.profiles:
        # 판본 이력과 current pointer를 **둘 다** 만든다. pointer만 두면 version을 고정한
        # Assessment가 나중에 그 판본을 직접 읽을 수 없고, 판본만 두면 새 Assessment가 어떤
        # Profile을 쓸지 정할 곳이 없다.
        published = {
            "PK": pk,
            "entity_type": "POLICY_PROFILE",
            "customer_id": customer_id,
            **profile.to_dict(),
        }
        items.append(
            {
                **published,
                "SK": f"POLICY_PROFILE#{profile.policy_profile_id}#VERSION#{profile.version}",
            }
        )
        items.append(
            {
                **published,
                "SK": f"POLICY_PROFILE#{profile.policy_profile_id}",
                "current_version": profile.version,
            }
        )
    return tuple(items)


def _is_profile_pointer(item: Mapping[str, object]) -> bool:
    """The `POLICY_PROFILE#<id>` item without a `#VERSION#` segment — the only movable key."""
    sk = str(item.get("SK", ""))
    return item.get("entity_type") == "POLICY_PROFILE" and "#VERSION#" not in sk


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, Mapping) else None
    code = details.get("Code") if isinstance(details, Mapping) else None
    return code if isinstance(code, str) else None
