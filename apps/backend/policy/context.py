"""Resolve an approved, deterministic Policy Context without exposing policy originals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import AssessmentPhase, PolicyProfile, PolicyRule, SourceReference


class PolicyNotFoundError(LookupError):
    """Raised when an approved profile references a rule that is unavailable."""


class PolicyCatalog(Protocol):
    """Customer-scoped read interface; authorization belongs to the Backend caller."""

    def get_profile(self, policy_profile_id: str) -> PolicyProfile | None: ...

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyContext:
    """Rule subset and traceable references safe to pass to an evaluator."""

    policy_profile_id: str
    policy_profile_version: str
    phase: AssessmentPhase
    resource_type: str
    rules: tuple[PolicyRule, ...]

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError("rules must not be empty")
        for rule in self.rules:
            if not isinstance(rule, PolicyRule):
                raise TypeError("rules must contain PolicyRule values")

    @property
    def source_references(self) -> tuple[SourceReference, ...]:
        """Return a de-duplicated evidence locator set in profile rule order."""
        references: list[SourceReference] = []
        for rule in self.rules:
            for reference in rule.source_references:
                if reference not in references:
                    references.append(reference)
        return tuple(references)


class PolicyContextResolver:
    """Apply Policy Profile allow-list and applicability filters deterministically."""

    def __init__(self, catalog: PolicyCatalog) -> None:
        if catalog is None:
            raise TypeError("catalog is required")
        self._catalog = catalog

    def resolve(
        self, *, policy_profile_id: str, phase: AssessmentPhase, resource_type: str
    ) -> PolicyContext:
        if not isinstance(policy_profile_id, str) or not policy_profile_id.strip():
            raise ValueError("policy_profile_id must be a non-empty string")
        if not isinstance(phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        if not isinstance(resource_type, str) or not resource_type.strip():
            raise ValueError("resource_type must be a non-empty string")
        profile = self._catalog.get_profile(policy_profile_id)
        if profile is None:
            raise PolicyNotFoundError("policy profile not found")
        rules = tuple(
            self._resolve_rule(reference.rule_id, reference.version)
            for reference in profile.rule_references
        )
        applicable = tuple(
            rule
            for rule in rules
            if phase in rule.applicable_phases and resource_type in rule.resource_types
        )
        if not applicable:
            raise PolicyNotFoundError("no applicable policy rules")
        return PolicyContext(
            policy_profile_id=profile.policy_profile_id,
            policy_profile_version=profile.version,
            phase=phase,
            resource_type=resource_type,
            rules=applicable,
        )

    def _resolve_rule(self, rule_id: str, version: str) -> PolicyRule:
        rule = self._catalog.get_rule(rule_id, version)
        if rule is None:
            raise PolicyNotFoundError("policy profile references an unavailable rule")
        if rule.rule_id != rule_id or rule.version != version:
            raise PolicyNotFoundError("policy catalog returned a rule outside profile version pin")
        return rule
