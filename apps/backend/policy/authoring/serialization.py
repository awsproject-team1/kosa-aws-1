"""Restore stored authoring outcomes into Contract values.

`rule_from_dict()`와 같은 이유로 복원 경로를 한곳에 모은다. `ExtractedRequirement`에 필드가
늘었는데 어느 한 복원 경로만 갱신되지 않으면, 리뷰 화면이 보여주는 후보와 저장된 후보가
조용히 달라진다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apps.backend.policy.serialization import rule_from_dict
from packages.contracts import (
    AcceptedRequirement,
    CandidateClassification,
    CandidateRejectionCode,
    ExtractedRequirement,
    RejectedRequirement,
    RuleCandidate,
    RuleEvaluationType,
    RuleLifecycle,
)


def requirement_from_dict(data: object) -> ExtractedRequirement:
    fields = _mapping(data, "extracted requirement")
    evaluation_type = fields.get("evaluation_type")
    return ExtractedRequirement(
        source_locators=_str_tuple(fields, "source_locators"),
        requirement=_string(fields, "requirement"),
        requirement_summary=_string(fields, "requirement_summary"),
        classification=CandidateClassification(_string(fields, "classification")),
        mapping_reason=_string(fields, "mapping_reason"),
        mapped_control_key=_optional_string(fields, "mapped_control_key"),
        resource_types=_str_tuple(fields, "resource_types"),
        evaluation_type=(
            None if evaluation_type is None else RuleEvaluationType(str(evaluation_type))
        ),
        applicability_semantics=_optional_string(fields, "applicability_semantics"),
        required_evidence=_str_tuple(fields, "required_evidence"),
        optional_evidence=_str_tuple(fields, "optional_evidence"),
        evaluation_rubric=_optional_string(fields, "evaluation_rubric"),
        severity_guidance=_optional_string(fields, "severity_guidance"),
        exception_semantics=_optional_string(fields, "exception_semantics"),
        compensating_control_semantics=_optional_string(fields, "compensating_control_semantics"),
    )


def accepted_from_dict(data: object) -> AcceptedRequirement:
    fields = _mapping(data, "accepted requirement")
    candidate = _mapping(fields.get("candidate"), "rule candidate")
    return AcceptedRequirement(
        requirement=requirement_from_dict(fields.get("requirement")),
        candidate=RuleCandidate(
            rule=rule_from_dict(candidate.get("rule")),
            lifecycle=RuleLifecycle(_string(candidate, "lifecycle")),
        ),
    )


def rejected_from_dict(data: object) -> RejectedRequirement:
    fields = _mapping(data, "rejected requirement")
    codes = fields.get("rejection_codes")
    if not isinstance(codes, Sequence) or isinstance(codes, (str, bytes)):
        raise TypeError("rejection_codes must be a list")
    return RejectedRequirement(
        requirement=requirement_from_dict(fields.get("requirement")),
        rejection_codes=tuple(CandidateRejectionCode(str(code)) for code in codes),
    )


def _mapping(data: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return data


def _string(fields: Mapping[str, object], name: str) -> str:
    value = fields.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_string(fields: Mapping[str, object], name: str) -> str | None:
    value = fields.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _str_tuple(fields: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = fields.get(name)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a list")
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError(f"{name} items must be strings")
    return tuple(value)
