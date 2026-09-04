"""Deserialize committed policy definitions into Contract values.

Rule 정의 파일은 저장소에 커밋되지만 정책 원문은 아니다 (ADR-0004). 여기서는 locator와
`content_sha256`만 읽고, 원문 문장은 어느 경로로도 다루지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping

from packages.contracts import (
    AssessmentPhase,
    PolicyControl,
    PolicyProfile,
    PolicyProfileSegment,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceKind,
    RuleEvaluationType,
    RuleSeverity,
    SourceReference,
)
from packages.contracts.remediation_policy import RemediationEligibility, RemediationRuleScope


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
        source_version=fields["source_version"],
        locator=fields["locator"],
        content_sha256=fields["content_sha256"],
    )


def rule_reference_from_dict(data: object) -> PolicyRuleReference:
    fields = _require_mapping(data, "rule reference")
    return PolicyRuleReference(rule_id=fields["rule_id"], version=fields["version"])


def rule_from_dict(data: object) -> PolicyRule:
    """Restore one Rule from any stored form: fixture JSON, DynamoDB item, or candidate item.

    실행 의미 필드는 optional이다 — legacy fixture Rule에는 없고, authoring이 만든 Rule에는 있다.
    복원 경로를 여기 하나로 모아 두어야, 새 필드를 추가했는데 어느 한 경로만 그것을 잃어버려
    승인된 Rule이 Runtime에서 legacy Rule처럼 평가되는 사고가 생기지 않는다.
    """
    fields = _require_mapping(data, "policy rule")
    evaluation_type = fields.get("evaluation_type")
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
        control_key=_optional_str(fields, "control_key"),
        control_catalog_version=_optional_str(fields, "control_catalog_version"),
        evaluation_type=(None if evaluation_type is None else RuleEvaluationType(evaluation_type)),
        applicability_semantics=_optional_str(fields, "applicability_semantics"),
        required_evidence=_optional_str_tuple(fields, "required_evidence"),
        optional_evidence=_optional_str_tuple(fields, "optional_evidence"),
        evaluation_rubric=_optional_str(fields, "evaluation_rubric"),
        severity_guidance=_optional_str(fields, "severity_guidance"),
        exception_semantics=_optional_str(fields, "exception_semantics"),
        compensating_control_semantics=_optional_str(fields, "compensating_control_semantics"),
    )


def _optional_str(fields: Mapping[str, object], name: str) -> str | None:
    """Read an optional string, treating an absent key and a stored null the same."""
    value = fields.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_str_tuple(fields: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = fields.get(name)
    if value is None:
        return ()
    entries = _require_sequence(value)
    for entry in entries:
        if not isinstance(entry, str):
            raise TypeError(f"{name} items must be strings")
    return tuple(entries)  # type: ignore[arg-type]


def profile_segment_from_dict(data: object) -> PolicyProfileSegment:
    fields = _require_mapping(data, "policy profile segment")
    return PolicyProfileSegment(
        kind=PolicySourceKind(fields["kind"]),
        source_id=fields["source_id"],
        source_version=fields["source_version"],
        rule_references=tuple(
            rule_reference_from_dict(reference)
            for reference in _require_sequence(fields["rule_references"])
        ),
    )


def profile_from_dict(data: object) -> PolicyProfile:
    """Restore a Profile, tolerating the shape published before Profiles carried segments."""
    fields = _require_mapping(data, "policy profile")
    segments = fields.get("segments")
    return PolicyProfile(
        policy_profile_id=fields["policy_profile_id"],
        version=fields["version"],
        rule_references=tuple(
            rule_reference_from_dict(reference)
            for reference in _require_sequence(fields["rule_references"])
        ),
        segments=(
            ()
            if segments is None
            else tuple(
                profile_segment_from_dict(segment) for segment in _require_sequence(segments)
            )
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


def remediation_scope_from_dict(data: object) -> RemediationRuleScope:
    fields = _require_mapping(data, "remediation scope")
    return RemediationRuleScope(
        rule_id=fields["rule_id"],
        version=fields["version"],
        eligibility=RemediationEligibility(fields["eligibility"]),
    )


def _require_mapping(data: object, field_name: str) -> Mapping[str, object]:
    """Accept any mapping so DynamoDB items and committed JSON share one restore path."""
    if not isinstance(data, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return data


def _require_sequence(data: object) -> list[object]:
    if not isinstance(data, (list, tuple)):
        raise TypeError("expected a list of values")
    return list(data)
