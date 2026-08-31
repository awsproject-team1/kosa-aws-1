"""Read-only, deterministic Policy Catalog adapters for M0 workers and fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from apps.backend.policy.context import PolicyNotFoundError
from apps.backend.policy.serialization import (
    profile_from_dict,
    rule_from_dict,
    source_from_dict,
)
from packages.contracts import PolicyProfile, PolicyRule, PolicySource


class InMemoryPolicyCatalog:
    """Immutable Profile/Rule lookup with explicit allow-list referential integrity."""

    def __init__(self, *, profiles: Iterable[PolicyProfile], rules: Iterable[PolicyRule]) -> None:
        self._profiles = _unique_by_id(profiles, "policy_profile_id", "profiles", PolicyProfile)
        self._rules = _unique_rules(rules)
        for profile in self._profiles.values():
            missing = {
                (reference.rule_id, reference.version) for reference in profile.rule_references
            }.difference(self._rules)
            if missing:
                raise PolicyNotFoundError("policy profile references an unavailable rule")

    def get_profile(self, policy_profile_id: str) -> PolicyProfile | None:
        return self._profiles.get(policy_profile_id)

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None:
        return self._rules.get((rule_id, version))


def load_m0_fixture_catalog(path: Path) -> tuple[PolicySource, InMemoryPolicyCatalog]:
    """Load the checked-in M0 policy fixture without exposing a policy original to workers."""
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    try:
        fixture = json.loads(path.read_text())
        if not isinstance(fixture, dict):
            raise ValueError("M0 policy fixture must be an object")
        source = source_from_dict(fixture["policy_source"])
        rule = rule_from_dict(fixture["rule"])
        profile = profile_from_dict(fixture["policy_profile"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("M0 policy fixture is invalid") from error
    return source, InMemoryPolicyCatalog(profiles=(profile,), rules=(rule,))


def _unique_by_id[T](
    values: Iterable[T], identifier: str, field_name: str, expected_type: type[T]
) -> dict[str, T]:
    index: dict[str, T] = {}
    for value in values:
        if not isinstance(value, expected_type):
            raise TypeError(f"{field_name} must contain {expected_type.__name__} values")
        key = getattr(value, identifier)
        if key in index:
            raise ValueError(f"{field_name} contains duplicate {identifier}")
        index[key] = value
    return index


def _unique_rules(values: Iterable[PolicyRule]) -> dict[tuple[str, str], PolicyRule]:
    index: dict[tuple[str, str], PolicyRule] = {}
    for rule in values:
        if not isinstance(rule, PolicyRule):
            raise TypeError("rules must contain PolicyRule values")
        key = (rule.rule_id, rule.version)
        if key in index:
            raise ValueError("rules contains duplicate rule_id and version")
        index[key] = rule
    return index
