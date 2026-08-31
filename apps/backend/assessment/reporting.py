"""Immutable Assessment-plan and result retrieval for Coverage reporting."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from apps.backend.assessment.coverage import calculate_coverage
from packages.contracts import (
    AssessmentCoverage,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
)


class AssessmentReportNotFoundError(LookupError):
    """Raised when an Assessment plan or its report data is unavailable."""


class AssessmentReportStoreError(RuntimeError):
    """Raised when report persistence cannot be safely read or written."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentEvaluationPlan:
    """Server-generated applicable Resource × Rule × Perspective count."""

    customer_id: str
    assessment_id: str
    planned_evaluations: int

    def __post_init__(self) -> None:
        for field_name in ("customer_id", "assessment_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if isinstance(self.planned_evaluations, bool) or not isinstance(
            self.planned_evaluations, int
        ):
            raise TypeError("planned_evaluations must be an integer")
        if self.planned_evaluations <= 0:
            raise ValueError("planned_evaluations must be greater than zero")


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentReport:
    """Results plus the Coverage calculated from their authoritative plan."""

    assessment_id: str
    results: tuple[EvaluationResult, ...]
    coverage: AssessmentCoverage
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.assessment_id, str) or not self.assessment_id.strip():
            raise ValueError("assessment_id must be a non-empty string")
        if not isinstance(self.results, tuple) or not all(
            isinstance(result, EvaluationResult) for result in self.results
        ):
            raise TypeError("results must be a tuple of EvaluationResult values")
        if not isinstance(self.coverage, AssessmentCoverage):
            raise TypeError("coverage must be an AssessmentCoverage")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ValueError("next_cursor must be a non-empty string or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "results": [result.to_dict() for result in self.results],
            "coverage": self.coverage.to_dict(),
            "next_cursor": self.next_cursor,
        }


class DynamoTable(Protocol):
    def put_item(self, **kwargs: object) -> object: ...

    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def query(self, **kwargs: object) -> Mapping[str, object]: ...


class DynamoDbAssessmentReportStore:
    """Persist a plan once and query only its customer-scoped Assessment prefix."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def put_plan_if_absent(self, plan: AssessmentEvaluationPlan) -> None:
        if not isinstance(plan, AssessmentEvaluationPlan):
            raise TypeError("plan must be an AssessmentEvaluationPlan")
        try:
            self._table.put_item(
                Item={
                    "PK": _customer_pk(plan.customer_id),
                    "SK": _plan_sk(plan.assessment_id),
                    "entity_type": "ASSESSMENT_EVALUATION_PLAN",
                    "customer_id": plan.customer_id,
                    "assessment_id": plan.assessment_id,
                    "planned_evaluations": plan.planned_evaluations,
                },
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except Exception as error:
            if _provider_error_code(error) == "ConditionalCheckFailedException":
                raise AssessmentReportStoreError(
                    "assessment evaluation plan already exists"
                ) from None
            raise AssessmentReportStoreError("assessment evaluation plan write failed") from None

    def get_report(self, *, customer_id: str, assessment_id: str) -> AssessmentReport:
        _non_empty_string(customer_id, "customer_id")
        _non_empty_string(assessment_id, "assessment_id")
        items = self._query_assessment_items(customer_id, assessment_id)
        plan = next(
            (
                item
                for item in items
                if isinstance(item, Mapping)
                and item.get("SK") == _plan_sk(assessment_id)
                and item.get("entity_type") == "ASSESSMENT_EVALUATION_PLAN"
            ),
            None,
        )
        if not isinstance(plan, Mapping):
            raise AssessmentReportNotFoundError("assessment evaluation plan not found")
        expected = _plan_from_item(plan, customer_id, assessment_id)
        results = tuple(
            _result_from_item(item, customer_id, assessment_id)
            for item in items
            if isinstance(item, Mapping) and item.get("entity_type") == "ASSESSMENT_RESULT"
        )
        return AssessmentReport(
            assessment_id=assessment_id,
            results=results,
            coverage=calculate_coverage(results=results, planned_evaluations=expected),
        )

    def get_assessment_job_id(self, *, customer_id: str, assessment_id: str) -> str:
        _non_empty_string(customer_id, "customer_id")
        _non_empty_string(assessment_id, "assessment_id")
        try:
            item = self._table.get_item(
                Key={"PK": _customer_pk(customer_id), "SK": f"ASSESSMENT#{assessment_id}"},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise AssessmentReportStoreError("assessment metadata read failed") from None
        if not isinstance(item, Mapping) or item.get("entity_type") != "ASSESSMENT":
            raise AssessmentReportNotFoundError("assessment not found")
        job_id = item.get("job_id")
        if item.get("customer_id") != customer_id or not isinstance(job_id, str) or not job_id:
            raise AssessmentReportStoreError("assessment metadata is invalid")
        return job_id

    def get_report_page(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        limit: int,
        cursor: str | None = None,
    ) -> AssessmentReport:
        """Return one results page while calculating Coverage over all stored results."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100")
        report = self.get_report(customer_id=customer_id, assessment_id=assessment_id)
        start_key = _decode_cursor(cursor, customer_id, assessment_id)
        arguments: dict[str, object] = {
            "KeyConditionExpression": "PK = :customer AND begins_with(SK, :results)",
            "ExpressionAttributeValues": {
                ":customer": _customer_pk(customer_id),
                ":results": _result_sk_prefix(assessment_id),
            },
            "Limit": limit,
        }
        if start_key is not None:
            arguments["ExclusiveStartKey"] = start_key
        try:
            response = self._table.query(**arguments)
        except Exception:
            raise AssessmentReportStoreError("assessment result page query failed") from None
        items = response.get("Items")
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise AssessmentReportStoreError("assessment result page is invalid")
        results = tuple(_result_from_item(item, customer_id, assessment_id) for item in items)
        next_cursor = _encode_cursor(response.get("LastEvaluatedKey"), customer_id, assessment_id)
        return AssessmentReport(
            assessment_id=assessment_id,
            results=results,
            coverage=report.coverage,
            next_cursor=next_cursor,
        )

    def _query_assessment_items(
        self, customer_id: str, assessment_id: str
    ) -> tuple[Mapping[str, object], ...]:
        items: list[Mapping[str, object]] = []
        start_key: Mapping[str, object] | None = None
        while True:
            arguments: dict[str, object] = {
                "KeyConditionExpression": "PK = :customer AND begins_with(SK, :assessment)",
                "ExpressionAttributeValues": {
                    ":customer": _customer_pk(customer_id),
                    ":assessment": f"ASSESSMENT#{assessment_id}#",
                },
            }
            if start_key is not None:
                arguments["ExclusiveStartKey"] = dict(start_key)
            try:
                response = self._table.query(**arguments)
            except Exception:
                raise AssessmentReportStoreError("assessment report query failed") from None
            page_items = response.get("Items")
            if not isinstance(page_items, list) or not all(
                isinstance(item, Mapping) for item in page_items
            ):
                raise AssessmentReportStoreError("assessment report query returned invalid items")
            items.extend(page_items)
            last_key = response.get("LastEvaluatedKey")
            if last_key is None:
                return tuple(items)
            if not isinstance(last_key, Mapping):
                raise AssessmentReportStoreError("assessment report cursor is invalid")
            start_key = last_key


def _plan_from_item(item: Mapping[str, object], customer_id: str, assessment_id: str) -> int:
    if item.get("customer_id") != customer_id or item.get("assessment_id") != assessment_id:
        raise AssessmentReportStoreError("assessment plan scope is invalid")
    value = item.get("planned_evaluations")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AssessmentReportStoreError("assessment plan is invalid")
    return value


def _result_from_item(
    item: Mapping[str, object], customer_id: str, assessment_id: str
) -> EvaluationResult:
    if item.get("customer_id") != customer_id or item.get("assessment_id") != assessment_id:
        raise AssessmentReportStoreError("assessment result scope is invalid")
    evidence = item.get("evidence_references")
    if not isinstance(evidence, list) or not all(
        isinstance(reference, str) for reference in evidence
    ):
        raise AssessmentReportStoreError("assessment result evidence is invalid")
    score = item.get("score")
    if isinstance(score, Decimal):
        score = float(score)
    try:
        return EvaluationResult(
            resource_id=item["resource_id"],
            rule_id=item["rule_id"],
            perspective=EvaluationPerspective(item["perspective"]),
            status=EvaluationStatus(item["status"]),
            severity=item["severity"],
            score=score,
            rationale=item["rationale"],
            evidence_references=tuple(evidence),
            rule_version=item["rule_version"],
            rubric_version=item["rubric_version"],
            model_profile_id=item["model_profile_id"],
        )
    except (KeyError, TypeError, ValueError):
        raise AssessmentReportStoreError("assessment result is invalid") from None


def _customer_pk(customer_id: str) -> str:
    return f"CUSTOMER#{customer_id}"


def _plan_sk(assessment_id: str) -> str:
    return f"ASSESSMENT#{assessment_id}#PLAN"


def _result_sk_prefix(assessment_id: str) -> str:
    return f"ASSESSMENT#{assessment_id}#RESULT#"


def _encode_cursor(value: object, customer_id: str, assessment_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AssessmentReportStoreError("assessment result page cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    if (
        pk != _customer_pk(customer_id)
        or not isinstance(sk, str)
        or not sk.startswith(_result_sk_prefix(assessment_id))
    ):
        raise AssessmentReportStoreError("assessment result page cursor is outside scope")
    raw = json.dumps({"PK": pk, "SK": sk}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    cursor: str | None, customer_id: str, assessment_id: str
) -> dict[str, str] | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("cursor must be a non-empty string or None")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None
    if not isinstance(value, dict) or set(value) != {"PK", "SK"}:
        raise ValueError("cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    if (
        pk != _customer_pk(customer_id)
        or not isinstance(sk, str)
        or not sk.startswith(_result_sk_prefix(assessment_id))
    ):
        raise ValueError("cursor is outside assessment scope")
    return {"PK": pk, "SK": sk}


def _non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    detail = response.get("Error") if isinstance(response, Mapping) else None
    code = detail.get("Code") if isinstance(detail, Mapping) else None
    return code if isinstance(code, str) else None
