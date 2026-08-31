"""Read-only, deterministic Policy Catalog adapters for M0 workers and fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from apps.backend.policy.context import PolicyNotFoundError
from packages.contracts import (
    AssessmentPhase,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceKind,
    RuleSeverity,
    SourceReference,
)


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
            raise ValueError
        source_data = fixture["policy_source"]
        rule_data = fixture["rule"]
        profile_data = fixture["policy_profile"]
        if not all(isinstance(value, dict) for value in (source_data, rule_data, profile_data)):
            raise ValueError
        source = PolicySource(
            source_id=source_data["source_id"],
            kind=PolicySourceKind(source_data["kind"]),
            title=source_data["title"],
            version=source_data["version"],
            artifact_id=source_data["artifact_id"],
            content_sha256=source_data["content_sha256"],
        )
        references = tuple(
            SourceReference(**reference) for reference in rule_data["source_references"]
        )
        rule = PolicyRule(
            rule_id=rule_data["rule_id"],
            version=rule_data["version"],
            title=rule_data["title"],
            severity=RuleSeverity(rule_data["severity"]),
            applicable_phases=tuple(
                AssessmentPhase(phase) for phase in rule_data["applicable_phases"]
            ),
            resource_types=tuple(rule_data["resource_types"]),
            source_references=references,
        )
        profile = PolicyProfile(
            policy_profile_id=profile_data["policy_profile_id"],
            version=profile_data["version"],
            rule_references=tuple(
                PolicyRuleReference(**reference) for reference in profile_data["rule_references"]
            ),
        )
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
