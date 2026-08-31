"""Customer-scoped, read-only DynamoDB adapter for the Policy Catalog.

Key layout는 `docs/DATABASE.md`의 item layout을 따른다.

- Policy profile: `PK=CUSTOMER#{customer_id}`, `SK=POLICY_PROFILE#{policy_profile_id}`
- Policy source:  `PK=CUSTOMER#{customer_id}`, `SK=POLICY_SOURCE#{source_id}#VERSION#{version}`
- Rule metadata:  `PK=CUSTOMER#{customer_id}`, `SK=RULE#{rule_id}#VERSION#{version}`

Catalog는 생성 시점에 하나의 `customer_id`에 묶이며 모든 조회가 그 PK만 사용한다. 다른
Customer의 항목은 이 어댑터로 표현할 수 없다. 권한 판정 자체는 Backend 호출자의 책임이다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from apps.backend.policy.serialization import (
    profile_from_dict,
    rule_from_dict,
    source_from_dict,
)
from apps.backend.repositories.errors import RepositoryError, StoredDataError
from packages.contracts import PolicyProfile, PolicyRule, PolicySource


class DynamoTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...


class DynamoDbPolicyCatalog:
    """Read-only `PolicyCatalog` backed by the shared metadata table."""

    def __init__(self, table: DynamoTable, *, customer_id: str) -> None:
        if table is None:
            raise TypeError("table is required")
        _require_non_empty_string(customer_id, "customer_id")
        self._table = table
        self._customer_id = customer_id

    @property
    def customer_id(self) -> str:
        return self._customer_id

    def get_profile(self, policy_profile_id: str) -> PolicyProfile | None:
        _require_non_empty_string(policy_profile_id, "policy_profile_id")
        item = self._read(f"POLICY_PROFILE#{policy_profile_id}")
        if item is None:
            return None
        profile = _parse(profile_from_dict, item, "policy profile")
        if profile.policy_profile_id != policy_profile_id:
            raise StoredDataError("stored policy profile identity is invalid")
        return profile

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None:
        _require_non_empty_string(rule_id, "rule_id")
        _require_non_empty_string(version, "version")
        item = self._read(f"RULE#{rule_id}#VERSION#{version}")
        if item is None:
            return None
        rule = _parse(rule_from_dict, item, "policy rule")
        if rule.rule_id != rule_id or rule.version != version:
            raise StoredDataError("stored policy rule version pin is invalid")
        return rule

    def get_source(self, source_id: str, version: str) -> PolicySource | None:
        _require_non_empty_string(source_id, "source_id")
        _require_non_empty_string(version, "version")
        item = self._read(f"POLICY_SOURCE#{source_id}#VERSION#{version}")
        if item is None:
            return None
        source = _parse(source_from_dict, item, "policy source")
        if source.source_id != source_id or source.version != version:
            raise StoredDataError("stored policy source identity is invalid")
        return source

    def _read(self, sort_key: str) -> Mapping[str, object] | None:
        """Read one item inside this catalog's customer partition."""
        try:
            response = self._table.get_item(
                Key={"PK": f"CUSTOMER#{self._customer_id}", "SK": sort_key},
                ConsistentRead=True,
            )
        except Exception:
            raise RepositoryError("policy catalog read failed") from None
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise StoredDataError("stored policy item is invalid")
        if item.get("customer_id") != self._customer_id:
            raise StoredDataError("stored policy item customer scope is invalid")
        return item


def _parse[T](builder: Callable[[object], T], item: Mapping[str, object], field_name: str) -> T:
    """Deserialize a stored item, reporting provider-neutral failures."""
    try:
        return builder(dict(item))
    except (KeyError, TypeError, ValueError) as error:
        raise StoredDataError(f"stored {field_name} is invalid") from error


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
