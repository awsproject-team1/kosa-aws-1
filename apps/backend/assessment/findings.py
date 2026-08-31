"""Deterministic Finding projection and immutable DynamoDB persistence."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Protocol

from packages.contracts import EvaluationResult, EvaluationStatus, Finding


class DynamoTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_item(self, **kwargs: object) -> object: ...


class FindingStoreError(RuntimeError):
    """Raised when a derived Finding cannot be persisted safely."""


class ImmutableFindingConflict(FindingStoreError):
    """Raised when a Finding identity already contains different content."""


_FOLLOW_UP_STATUSES = frozenset(
    {
        EvaluationStatus.FAIL,
        EvaluationStatus.MANUAL_REVIEW,
        EvaluationStatus.INSUFFICIENT_EVIDENCE,
    }
)


def finding_from_result(result: EvaluationResult) -> Finding | None:
    """Return the stable actionable projection, or None for non-findings."""
    if not isinstance(result, EvaluationResult):
        raise TypeError("result must be an EvaluationResult")
    if result.status not in _FOLLOW_UP_STATUSES:
        return None
    identity = "\x1f".join(
        (result.resource_id, result.rule_id, result.rule_version, result.perspective.value)
    )
    return Finding(
        finding_id=f"finding-{sha256(identity.encode()).hexdigest()[:24]}",
        resource_id=result.resource_id,
        rule_id=result.rule_id,
        rule_version=result.rule_version,
        perspective=result.perspective,
        status=result.status,
        severity=result.severity,
        score=result.score,
        rationale=result.rationale,
        evidence_references=result.evidence_references,
    )


class DynamoDbFindingStore:
    """Persist derived Findings under the documented Assessment prefix."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def put_if_absent(self, *, customer_id: str, assessment_id: str, finding: Finding) -> None:
        _non_empty_string(customer_id, "customer_id")
        _non_empty_string(assessment_id, "assessment_id")
        if not isinstance(finding, Finding):
            raise TypeError("finding must be a Finding")
        item = {
            "PK": f"CUSTOMER#{customer_id}",
            "SK": f"ASSESSMENT#{assessment_id}#FINDING#{finding.finding_id}",
            "entity_type": "FINDING",
            "customer_id": customer_id,
            "assessment_id": assessment_id,
            **finding.to_dict(),
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except Exception as error:
            if _provider_error_code(error) != "ConditionalCheckFailedException":
                raise FindingStoreError("finding write failed") from None
            if self._existing_item_matches(item):
                return
            raise ImmutableFindingConflict(
                "finding key already contains different content"
            ) from None

    def _existing_item_matches(self, expected: dict[str, object]) -> bool:
        try:
            response = self._table.get_item(
                Key={"PK": expected["PK"], "SK": expected["SK"]}, ConsistentRead=True
            )
        except Exception:
            raise FindingStoreError("finding read after conflict failed") from None
        existing = response.get("Item")
        return isinstance(existing, Mapping) and dict(existing) == expected


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, Mapping) else None
    code = details.get("Code") if isinstance(details, Mapping) else None
    return code if isinstance(code, str) else None


def _non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
