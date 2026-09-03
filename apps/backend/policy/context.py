"""Resolve an approved, deterministic Policy Context without exposing policy originals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.contracts import AssessmentPhase, PolicyProfile, PolicyRule, SourceReference


class PolicyNotFoundError(LookupError):
    """Raised when an approved profile references a rule that is unavailable."""


class NoApplicablePolicyRulesError(PolicyNotFoundError):
    """The Profile exists and resolved, but no Rule applies to this phase and resource type.

    Profile이 없는 것과는 다른 답이다. 전자는 설정 오류이고, 이 경우는 "이 유형에는 평가할 것이
    없다"이므로 호출자가 그 유형의 work를 만들지 않는 것이 맞다 — 예를 들어 MANUAL Rule이
    하나도 승인되지 않은 Profile에는 governance 좌표가 없다.
    """


class PolicyCatalog(Protocol):
    """Customer-scoped read interface; authorization belongs to the Backend caller."""

    def get_profile(
        self, policy_profile_id: str, version: str | None = None
    ) -> PolicyProfile | None: ...

    def get_rule(self, rule_id: str, version: str) -> PolicyRule | None: ...


# 정책 근거가 아닌 Evidence의 namespace. Resource 상태 근거(IaC/AWS 조회 결과)는 Policy Source
# locator가 아니므로 Profile allow-list로 검증할 수 없고, 대신 이 접두사로만 표현한다.
RESOURCE_EVIDENCE_PREFIXES = ("aws:", "terraform:", "s3://")


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

    @property
    def policy_evidence_references(self) -> frozenset[str]:
        """Canonical policy Evidence 문자열의 allow-list."""
        return frozenset(reference.evidence_reference for reference in self.source_references)

    def allows_evidence(self, reference: object) -> bool:
        """Whether one Evidence reference is permitted for this Context.

        정책 근거는 이 Context가 실제로 포함한 `SourceReference`의 canonical 형식이어야 한다.
        Resource 상태 근거는 `RESOURCE_EVIDENCE_PREFIXES` namespace로만 표현한다. 둘 다 아니면
        평가기가 승인 범위 밖의 근거를 만들어낸 것이다.
        """
        if not isinstance(reference, str) or not reference.strip():
            return False
        if reference in self.policy_evidence_references:
            return True
        return reference.startswith(RESOURCE_EVIDENCE_PREFIXES)


class PolicyContextResolver:
    """Apply Policy Profile allow-list and applicability filters deterministically."""

    def __init__(self, catalog: PolicyCatalog) -> None:
        if catalog is None:
            raise TypeError("catalog is required")
        self._catalog = catalog

    def resolve(
        self,
        *,
        policy_profile_id: str,
        phase: AssessmentPhase,
        resource_type: str,
        expected_profile_version: str | None = None,
    ) -> PolicyContext:
        """Resolve the approved Rule subset, optionally pinned to a Profile version.

        Rule version이 고정돼도 Profile이 Rule 선택 경계이므로, 비동기 Job은 승인 시점의
        Profile version을 함께 고정해야 한다. `expected_profile_version`을 주면 그 사이에
        Profile이 교체된 경우 다른 allow-list로 평가하지 않고 실패한다.
        """
        if not isinstance(policy_profile_id, str) or not policy_profile_id.strip():
            raise ValueError("policy_profile_id must be a non-empty string")
        if expected_profile_version is not None and (
            not isinstance(expected_profile_version, str) or not expected_profile_version.strip()
        ):
            raise ValueError("expected_profile_version must be a non-empty string or None")
        if not isinstance(phase, AssessmentPhase):
            raise TypeError("phase must be an AssessmentPhase")
        if not isinstance(resource_type, str) or not resource_type.strip():
            raise ValueError("resource_type must be a non-empty string")
        # pin된 version이 있으면 **그 version item을 직접 읽는다.** current pointer를 읽고
        # version을 대조하면, 그 사이에 새 Profile이 게시된 Assessment는 조용히 실패하는 것이
        # 아니라 아예 평가되지 못한다. 판본은 immutable하므로 직접 조회가 항상 가능하다.
        profile = self._catalog.get_profile(policy_profile_id, expected_profile_version)
        if profile is None:
            raise self._missing_profile(policy_profile_id, expected_profile_version)
        if expected_profile_version is not None and profile.version != expected_profile_version:
            raise PolicyNotFoundError(
                "policy profile version changed since the assessment was approved"
            )
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
            raise NoApplicablePolicyRulesError("no applicable policy rules")
        return PolicyContext(
            policy_profile_id=profile.policy_profile_id,
            policy_profile_version=profile.version,
            phase=phase,
            resource_type=resource_type,
            rules=applicable,
        )

    def _missing_profile(
        self, policy_profile_id: str, expected_profile_version: str | None
    ) -> PolicyNotFoundError:
        """Say which of the two failures happened: no such Profile, or a replaced version.

        둘을 같은 메시지로 뭉치면 운영에서 원인을 가릴 수 없다. "Profile이 없다"는 설정 오류이고,
        "고정한 판본이 사라졌다"는 게시 이력 문제다. 이 조회는 실패 경로에서만 한 번 더 일어난다.
        """
        if expected_profile_version is None:
            return PolicyNotFoundError("policy profile not found")
        if self._catalog.get_profile(policy_profile_id) is None:
            return PolicyNotFoundError("policy profile not found")
        return PolicyNotFoundError(
            "policy profile version changed since the assessment was approved"
        )

    def _resolve_rule(self, rule_id: str, version: str) -> PolicyRule:
        rule = self._catalog.get_rule(rule_id, version)
        if rule is None:
            raise PolicyNotFoundError("policy profile references an unavailable rule")
        if rule.rule_id != rule_id or rule.version != version:
            raise PolicyNotFoundError("policy catalog returned a rule outside profile version pin")
        return rule
