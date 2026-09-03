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
from packages.common.errors import RepositoryError, StoredDataError
from packages.contracts import PolicyProfile, PolicyRule, PolicySource, RuleLifecycle

#: Rule item이 반드시 선언해야 하는 entity type. bootstrap과 승인 write가 같은 값을 쓴다.
_POLICY_RULE_ENTITY_TYPE = "POLICY_RULE"


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

    def get_profile(
        self, policy_profile_id: str, version: str | None = None
    ) -> PolicyProfile | None:
        """Read a Profile — the immutable version item when pinned, the pointer otherwise.

        Assessment는 실행 시점의 version을 고정하고 그 판본을 **직접** 읽는다. current pointer를
        따라간 뒤 version을 대조하는 방식은, 실행 중 새 Profile이 게시되면 이미 계획된 평가를
        더 이상 완료하지 못하게 만든다. 판본 item은 immutable하므로 언제 읽어도 같은 것이 나온다.
        """
        _require_non_empty_string(policy_profile_id, "policy_profile_id")
        if version is not None:
            _require_non_empty_string(version, "version")
        sort_key = f"POLICY_PROFILE#{policy_profile_id}"
        if version is not None:
            sort_key = f"{sort_key}#VERSION#{version}"
        item = self._read(sort_key)
        if item is None:
            return None
        profile = _parse(profile_from_dict, item, "policy profile")
        if profile.policy_profile_id != policy_profile_id:
            raise StoredDataError("stored policy profile identity is invalid")
        if version is not None and profile.version != version:
            raise StoredDataError("stored policy profile version pin is invalid")
        return profile

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None:
        """Return one approved Rule version from this customer's partition.

        `entity_type`과 `lifecycle`을 함께 확인한다. key 모양만 맞으면 통과시키면, 승인 경계를
        거치지 않고 partition에 들어온 item이 Runtime에서 Rule로 평가된다 — 그것이 이 어댑터가
        막아야 하는 유일한 사고다. 미승인 Rule은 "없음"이 아니라 **오류**로 다룬다: 조용히
        None을 돌려주면 Profile이 참조하는 Rule이 사라진 것과 구별되지 않는다.
        """
        _require_non_empty_string(rule_id, "rule_id")
        _require_non_empty_string(version, "version")
        item = self._read(f"RULE#{rule_id}#VERSION#{version}")
        if item is None:
            return None
        if item.get("entity_type") != _POLICY_RULE_ENTITY_TYPE:
            raise StoredDataError("stored policy rule entity type is invalid")
        if item.get("lifecycle") != RuleLifecycle.APPROVED.value:
            raise StoredDataError("stored policy rule is not approved")
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
