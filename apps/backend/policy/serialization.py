"""Deserialize committed policy definitions into Contract values.

Rule 정의 파일은 저장소에 커밋되지만 정책 원문은 아니다 (ADR-0004). 여기서는 locator와
`content_sha256`만 읽고, 원문 문장은 어느 경로로도 다루지 않는다.
"""

from __future__ import annotations

from packages.contracts import (
    AssessmentPhase,
    PolicyControl,
    PolicyProfile,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceKind,
    RuleSeverity,
    SourceReference,
)


def source_from_dict(data: object) -> PolicySource:
    fields = _require_mapping(data, "policy source")
    return PolicySource(
        source_id=fields["source_id"],
        kind=PolicySourceKind(fields["kind"]),
        title=fields["title"],
        version=fields["version"],
        artifact_id=fields["artifact_id"],
        content_sha256=fields["content_sha256"],
    )


def source_reference_from_dict(data: object) -> SourceReference:
    fields = _require_mapping(data, "source reference")
    return SourceReference(
        source_id=fields["source_id"],
        locator=fields["locator"],
        content_sha256=fields["content_sha256"],
    )


def rule_reference_from_dict(data: object) -> PolicyRuleReference:
    fields = _require_mapping(data, "rule reference")
    return PolicyRuleReference(rule_id=fields["rule_id"], version=fields["version"])


def rule_from_dict(data: object) -> PolicyRule:
    fields = _require_mapping(data, "policy rule")
    return PolicyRule(
        rule_id=fields["rule_id"],
        version=fields["version"],
        title=fields["title"],
        severity=RuleSeverity(fields["severity"]),
        applicable_phases=tuple(
            AssessmentPhase(phase) for phase in _require_sequence(fields["applicable_phases"])
        ),
        resource_types=tuple(_require_sequence(fields["resource_types"])),
        source_references=tuple(
            source_reference_from_dict(reference)
            for reference in _require_sequence(fields["source_references"])
        ),
    )


def profile_from_dict(data: object) -> PolicyProfile:
    fields = _require_mapping(data, "policy profile")
    return PolicyProfile(
        policy_profile_id=fields["policy_profile_id"],
        version=fields["version"],
        rule_references=tuple(
            rule_reference_from_dict(reference)
            for reference in _require_sequence(fields["rule_references"])
        ),
    )


def control_from_dict(data: object) -> PolicyControl:
    fields = _require_mapping(data, "policy control")
    return PolicyControl(
        control_id=fields["control_id"],
        title=fields["title"],
        source_reference=source_reference_from_dict(fields["source_reference"]),
        rule_references=tuple(
            rule_reference_from_dict(reference)
            for reference in _require_sequence(fields["rule_references"])
        ),
    )


def _require_mapping(data: object, field_name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise TypeError(f"{field_name} must be an object")
    return data


def _require_sequence(data: object) -> list[object]:
    if not isinstance(data, list):
        raise TypeError("expected a list of values")
    return data
