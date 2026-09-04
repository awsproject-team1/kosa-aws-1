"""Immutable Assessment-plan and result retrieval for Coverage reporting."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from apps.backend.assessment.coverage import calculate_coverage
from apps.backend.assessment.readiness import (
    calculate_readiness_score,
    calculate_segment_readiness,
)

# Import the display-only suppression note directly from its module rather than the
# policy package root: policy.remediation depends only on packages.contracts, so this
# stays acyclic (assessment already depends on policy, never the reverse).
from apps.backend.policy.remediation import FindingSuppression
from packages.contracts import (
    AssessmentCoverage,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    Finding,
    PlannedEvaluation,
    PolicyProfile,
    ReadinessScore,
    SegmentReadinessScore,
)


class AssessmentReportNotFoundError(LookupError):
    """Raised when an Assessment plan or its report data is unavailable."""


class AssessmentReportStoreError(RuntimeError):
    """Raised when report persistence cannot be safely read or written."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentEvaluationPlan:
    """Server-generated applicable Resource × Rule × Perspective set.

    The set is the plan; the Coverage denominator is derived from it rather than
    stored beside it, so the count and the set can never disagree (ADR-0020 §5).
    """

    customer_id: str
    assessment_id: str
    planned_coordinates: tuple[PlannedEvaluation, ...]

    def __post_init__(self) -> None:
        for field_name in ("customer_id", "assessment_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.planned_coordinates, tuple) or not self.planned_coordinates:
            raise ValueError("planned_coordinates must be a non-empty tuple")
        if not all(isinstance(value, PlannedEvaluation) for value in self.planned_coordinates):
            raise TypeError("planned_coordinates must contain PlannedEvaluation values")
        if len(set(self.planned_coordinates)) != len(self.planned_coordinates):
            raise ValueError("planned_coordinates must not contain duplicates")

    @property
    def planned_evaluations(self) -> int:
        return len(self.planned_coordinates)


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentReport:
    """Results plus the Coverage calculated from their authoritative plan."""

    assessment_id: str
    results: tuple[EvaluationResult, ...]
    findings: tuple[Finding, ...]
    coverage: AssessmentCoverage
    readiness_score: ReadinessScore | None
    #: 이 Assessment의 Profile이 여러 원본에 걸칠 때, 원본별 준비도. 사내 정책과 ISMS-P를 한
    #: Profile로 평가해도 두 점수는 합치지 않는다 — 합친 숫자는 어느 기준에 대한 답도 아니다.
    #: 원본을 구분하지 않고 게시된 Profile은 빈 값이며, 그 경우 `readiness_score` 하나만 쓴다.
    segment_readiness: tuple[SegmentReadinessScore, ...] = ()
    next_cursor: str | None = None
    findings_next_cursor: str | None = None
    # Read-time suppression notes for the findings on this page (ADR-0020 §6).
    # A display-only join, never persisted; the empty default means "no exception
    # is in force", which is the correct shape for stores that do not supply them.
    suppressions: tuple[FindingSuppression, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.assessment_id, str) or not self.assessment_id.strip():
            raise ValueError("assessment_id must be a non-empty string")
        if not isinstance(self.results, tuple) or not all(
            isinstance(result, EvaluationResult) for result in self.results
        ):
            raise TypeError("results must be a tuple of EvaluationResult values")
        if not isinstance(self.findings, tuple) or not all(
            isinstance(finding, Finding) for finding in self.findings
        ):
            raise TypeError("findings must be a tuple of Finding values")
        if not isinstance(self.coverage, AssessmentCoverage):
            raise TypeError("coverage must be an AssessmentCoverage")
        if self.readiness_score is not None and not isinstance(
            self.readiness_score, ReadinessScore
        ):
            raise TypeError("readiness_score must be a ReadinessScore or None")
        if not isinstance(self.segment_readiness, tuple) or not all(
            isinstance(entry, SegmentReadinessScore) for entry in self.segment_readiness
        ):
            raise TypeError("segment_readiness must be a tuple of SegmentReadinessScore values")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str) or not self.next_cursor
        ):
            raise ValueError("next_cursor must be a non-empty string or None")
        if self.findings_next_cursor is not None and (
            not isinstance(self.findings_next_cursor, str) or not self.findings_next_cursor
        ):
            raise ValueError("findings_next_cursor must be a non-empty string or None")
        if not isinstance(self.suppressions, tuple) or not all(
            isinstance(note, FindingSuppression) for note in self.suppressions
        ):
            raise TypeError("suppressions must be a tuple of FindingSuppression values")

    def with_suppressions(self, suppressions: tuple[FindingSuppression, ...]) -> AssessmentReport:
        """Return a copy carrying the read-time suppression notes for this page.

        The store builds the report from durable facts; the read-time join happens
        in the API service, which has the customer's exceptions and the read clock.
        Returning a copy keeps the store pure and the report immutable.
        """
        return replace(self, suppressions=suppressions)

    def to_dict(self) -> dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "results": [result.to_dict() for result in self.results],
            "findings": [finding.to_dict() for finding in self.findings],
            "coverage": self.coverage.to_dict(),
            "readiness_score": (
                self.readiness_score.to_dict() if self.readiness_score is not None else None
            ),
            "segment_readiness": [entry.to_dict() for entry in self.segment_readiness],
            "next_cursor": self.next_cursor,
            "findings_next_cursor": self.findings_next_cursor,
            "suppressions": [note.to_dict() for note in self.suppressions],
        }


class DynamoTable(Protocol):
    def put_item(self, **kwargs: object) -> object: ...

    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def query(self, **kwargs: object) -> Mapping[str, object]: ...


class PolicyProfileReader(Protocol):
    """The one Catalog read the report needs: which Rules came from which policy source."""

    def get_profile(
        self, policy_profile_id: str, version: str | None = None
    ) -> PolicyProfile | None: ...


class DynamoDbAssessmentReportStore:
    """Persist a plan once and query only its customer-scoped Assessment prefix.

    `policy_catalog_factory`가 주어지면 보고서에 원본별 준비도를 함께 담는다. Assessment item이
    이미 자기 Profile 판본을 고정해 두었으므로(`policy_profile_id`/`policy_profile_version`),
    그 판본을 읽어 Rule이 어느 원본에서 왔는지 알아낸다 — 평가 계획이나 결과 item의 형태는
    건드리지 않는다. 주어지지 않으면 지금까지처럼 전체 점수 하나만 낸다.

    Catalog는 호출자의 `customer_id`로 만든다. Store 하나가 여러 고객의 보고서를 읽으므로,
    생성 시점에 한 고객으로 묶인 Catalog를 그대로 들고 있으면 다른 고객의 Profile을 읽는다.
    """

    def __init__(
        self,
        table: DynamoTable,
        *,
        policy_catalog_factory: Callable[[str], PolicyProfileReader] | None = None,
    ) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table
        self._policy_catalog_factory = policy_catalog_factory

    def _rule_kinds(self, customer_id: str, assessment_id: str) -> dict[str, tuple[str, ...]]:
        """Read this Assessment's pinned Profile and map each Rule to its policy origins.

        분류에 실패하면 빈 map을 돌려준다. 원본별 점수는 보고의 정밀도이지 접근 통제가 아니므로,
        Profile을 읽지 못했다고 보고서 자체를 못 읽게 만들지 않는다. 빈 map은 "구분되지 않음"이며
        원본 구분 없이 게시된 Profile과 같은 답이다.
        """
        if self._policy_catalog_factory is None:
            return {}
        try:
            item = self._table.get_item(
                Key={"PK": _customer_pk(customer_id), "SK": f"ASSESSMENT#{assessment_id}"},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            return {}
        if not isinstance(item, Mapping) or item.get("customer_id") != customer_id:
            return {}
        policy_profile_id = item.get("policy_profile_id")
        policy_profile_version = item.get("policy_profile_version")
        if not isinstance(policy_profile_id, str) or not policy_profile_id:
            return {}
        if not isinstance(policy_profile_version, str) or not policy_profile_version:
            # 판본을 고정하지 않은 legacy Assessment다. current pointer를 따라가면 그 사이에
            # 교체된 Profile로 점수를 나누게 되므로, 나누지 않는다.
            return {}
        try:
            catalog = self._policy_catalog_factory(customer_id)
            profile = catalog.get_profile(policy_profile_id, policy_profile_version)
        except Exception:
            return {}
        if profile is None:
            return {}
        return {
            rule_id: tuple(kind.value for kind in kinds)
            for rule_id, kinds in profile.rule_kinds().items()
        }

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
                    "planned_coordinates": [
                        coordinate.to_dict() for coordinate in plan.planned_coordinates
                    ],
                    "completed_evaluations": 0,
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
        expected, completed, planned = _plan_from_item(plan, customer_id, assessment_id)
        results = tuple(
            _result_from_item(item, customer_id, assessment_id)
            for item in items
            if isinstance(item, Mapping) and item.get("entity_type") == "ASSESSMENT_RESULT"
        )
        findings = tuple(
            _finding_from_item(item, customer_id, assessment_id)
            for item in items
            if isinstance(item, Mapping) and item.get("entity_type") == "FINDING"
        )
        # Non-transactional/local stores cannot atomically maintain the new
        # counter; retain the scan fallback while a zero counter has results.
        if completed is None or (completed == 0 and results):
            coverage = calculate_coverage(results=results, planned_evaluations=expected)
        else:
            coverage = AssessmentCoverage(
                planned_evaluations=expected, completed_evaluations=completed
            )
        readiness_score = (
            None
            if planned is None
            else calculate_readiness_score(results=results, planned_evaluations=planned)
        )
        return AssessmentReport(
            assessment_id=assessment_id,
            results=results,
            findings=findings,
            coverage=coverage,
            readiness_score=readiness_score,
            segment_readiness=(
                ()
                if planned is None
                else calculate_segment_readiness(
                    results=results,
                    planned_evaluations=planned,
                    rule_kinds=self._rule_kinds(customer_id, assessment_id),
                )
            ),
        )

    def get_planned_evaluations(
        self, *, customer_id: str, assessment_id: str
    ) -> tuple[PlannedEvaluation, ...]:
        """Return the durable planned set the comparison boundary compares against.

        A plan that predates the stored set is not silently reconstructed: the set
        is not recoverable from results, so the caller must see the absence rather
        than compare against a fabricated plan (ADR-0020 §5).
        """
        _non_empty_string(customer_id, "customer_id")
        _non_empty_string(assessment_id, "assessment_id")
        plan = self._get_plan(customer_id=customer_id, assessment_id=assessment_id)
        _, _, planned = _plan_from_item(plan, customer_id, assessment_id)
        if planned is None:
            raise AssessmentReportNotFoundError("assessment plan has no planned evaluations")
        return planned

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
        findings_cursor: str | None = None,
    ) -> AssessmentReport:
        """Return independently pageable results/findings without scanning incomplete reports.

        New plans carry an immutable completion counter.  That makes Coverage a
        single strongly-consistent plan read while work is in progress; the
        legacy no-counter path deliberately retains the old full scan until all
        existing plans have drained.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100")
        plan = self._get_plan(customer_id=customer_id, assessment_id=assessment_id)
        expected, completed, planned = _plan_from_item(plan, customer_id, assessment_id)
        segment_readiness: tuple[SegmentReadinessScore, ...] = ()
        if completed is None:
            # Plans written before the transactional counter need a temporary
            # compatibility scan to keep their Coverage semantics unchanged.
            report = self.get_report(customer_id=customer_id, assessment_id=assessment_id)
            coverage, readiness_score = report.coverage, report.readiness_score
            segment_readiness = report.segment_readiness
        else:
            coverage = AssessmentCoverage(
                planned_evaluations=expected, completed_evaluations=completed
            )
            readiness_score = None
            if completed == 0:
                # A non-transactional/local writer may not have a counter
                # update primitive; recover its exact coverage until it does.
                existing_results = tuple(
                    _result_from_item(item, customer_id, assessment_id)
                    for item in self._query_prefix_items(
                        customer_id, _result_sk_prefix(assessment_id)
                    )
                )
                if existing_results:
                    coverage = calculate_coverage(
                        results=existing_results, planned_evaluations=expected
                    )
            # Readiness is intentionally unavailable until every applicable
            # evaluation is durable.  Once complete, derive it from all results
            # rather than trusting any page-local slice.
            if completed == expected and planned is not None:
                completed_results = tuple(
                    _result_from_item(item, customer_id, assessment_id)
                    for item in self._query_prefix_items(
                        customer_id, _result_sk_prefix(assessment_id)
                    )
                )
                readiness_score = calculate_readiness_score(
                    results=completed_results, planned_evaluations=planned
                )
                # 원본별 점수도 같은 전체 결과에서 낸다. 페이지 조각으로 계산하면 그 페이지에
                # 무엇이 실렸는지에 따라 점수가 달라진다.
                segment_readiness = calculate_segment_readiness(
                    results=completed_results,
                    planned_evaluations=planned,
                    rule_kinds=self._rule_kinds(customer_id, assessment_id),
                )
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
        finding_items, findings_next_cursor = self._query_page(
            customer_id=customer_id,
            assessment_id=assessment_id,
            prefix=_finding_sk_prefix(assessment_id),
            cursor=findings_cursor,
            limit=limit,
        )
        return AssessmentReport(
            assessment_id=assessment_id,
            results=results,
            findings=tuple(
                _finding_from_item(item, customer_id, assessment_id) for item in finding_items
            ),
            coverage=coverage,
            readiness_score=readiness_score,
            segment_readiness=segment_readiness,
            next_cursor=next_cursor,
            findings_next_cursor=findings_next_cursor,
        )

    def _query_page(
        self, *, customer_id: str, assessment_id: str, prefix: str, cursor: str | None, limit: int
    ) -> tuple[tuple[Mapping[str, object], ...], str | None]:
        start = _decode_cursor(cursor, customer_id, assessment_id, prefix)
        arguments: dict[str, object] = {
            "KeyConditionExpression": "PK = :customer AND begins_with(SK, :results)",
            "ExpressionAttributeValues": {
                ":customer": _customer_pk(customer_id),
                ":results": prefix,
            },
            "Limit": limit,
        }
        if start is not None:
            arguments["ExclusiveStartKey"] = start
        try:
            response = self._table.query(**arguments)
        except Exception:
            raise AssessmentReportStoreError("assessment page query failed") from None
        items = response.get("Items")
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise AssessmentReportStoreError("assessment page is invalid")
        return tuple(items), _encode_cursor(
            response.get("LastEvaluatedKey"), customer_id, assessment_id, prefix
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

    def _get_plan(self, *, customer_id: str, assessment_id: str) -> Mapping[str, object]:
        try:
            response = self._table.get_item(
                Key={"PK": _customer_pk(customer_id), "SK": _plan_sk(assessment_id)},
                ConsistentRead=True,
            )
        except Exception:
            raise AssessmentReportStoreError("assessment evaluation plan read failed") from None
        item = response.get("Item")
        if not isinstance(item, Mapping) or item.get("entity_type") != "ASSESSMENT_EVALUATION_PLAN":
            raise AssessmentReportNotFoundError("assessment evaluation plan not found")
        return item

    def _query_prefix_items(
        self, customer_id: str, prefix: str
    ) -> tuple[Mapping[str, object], ...]:
        items: list[Mapping[str, object]] = []
        start_key: Mapping[str, object] | None = None
        while True:
            arguments: dict[str, object] = {
                "KeyConditionExpression": "PK = :customer AND begins_with(SK, :results)",
                "ExpressionAttributeValues": {
                    ":customer": _customer_pk(customer_id),
                    ":results": prefix,
                },
            }
            if start_key is not None:
                arguments["ExclusiveStartKey"] = dict(start_key)
            try:
                response = self._table.query(**arguments)
            except Exception:
                raise AssessmentReportStoreError("assessment results query failed") from None
            page_items = response.get("Items")
            if not isinstance(page_items, list) or not all(
                isinstance(item, Mapping) for item in page_items
            ):
                raise AssessmentReportStoreError("assessment results query returned invalid items")
            items.extend(page_items)
            last_key = response.get("LastEvaluatedKey")
            if last_key is None:
                return tuple(items)
            if not isinstance(last_key, Mapping):
                raise AssessmentReportStoreError("assessment results cursor is invalid")
            start_key = last_key


def _plan_from_item(
    item: Mapping[str, object], customer_id: str, assessment_id: str
) -> tuple[int, int | None, tuple[PlannedEvaluation, ...] | None]:
    if item.get("customer_id") != customer_id or item.get("assessment_id") != assessment_id:
        raise AssessmentReportStoreError("assessment plan scope is invalid")
    value = _stored_int(item.get("planned_evaluations"))
    if value is None or value <= 0:
        raise AssessmentReportStoreError("assessment plan is invalid")
    coordinates = _plan_coordinates_from_item(item)
    if coordinates is not None and len(coordinates) != value:
        raise AssessmentReportStoreError("assessment plan count does not match its coordinates")
    completed = item.get("completed_evaluations")
    if completed is None:
        return value, None, coordinates
    completed = _stored_int(completed)
    if completed is None or not 0 <= completed <= value:
        raise AssessmentReportStoreError("assessment completed counter is invalid")
    return value, completed, coordinates


def _stored_int(value: object) -> int | None:
    """Normalize a stored integer, accepting the Decimal that boto3 resources return.

    The DynamoDB resource client unmarshals Number attributes as decimal.Decimal, so a
    plain isinstance(value, int) check rejects a legitimately stored count. Accept int
    and integral Decimal, reject bool and any fractional or non-numeric value.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _plan_coordinates_from_item(
    item: Mapping[str, object],
) -> tuple[PlannedEvaluation, ...] | None:
    """Return the planned set, or None for a plan written before it was stored.

    A plan without the set cannot be compared or scored — the count it does carry
    cannot tell a missing evaluation from an unplanned one that replaced it. The
    read paths degrade to "readiness unavailable" rather than reconstructing a set
    that is not recoverable from results (ADR-0020 §5).
    """
    raw = item.get("planned_coordinates")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise AssessmentReportStoreError("assessment plan coordinates are invalid")
    coordinates: list[PlannedEvaluation] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise AssessmentReportStoreError("assessment plan coordinates are invalid")
        resource_id = entry.get("resource_id")
        rule_id = entry.get("rule_id")
        perspective = entry.get("perspective")
        if (
            not isinstance(resource_id, str)
            or not isinstance(rule_id, str)
            or not isinstance(perspective, str)
        ):
            raise AssessmentReportStoreError("assessment plan coordinates are invalid")
        try:
            coordinates.append(
                PlannedEvaluation(
                    resource_id=resource_id,
                    rule_id=rule_id,
                    perspective=EvaluationPerspective(perspective),
                )
            )
        except (TypeError, ValueError):
            raise AssessmentReportStoreError("assessment plan coordinates are invalid") from None
    if len(set(coordinates)) != len(coordinates):
        raise AssessmentReportStoreError("assessment plan coordinates contain duplicates")
    return tuple(coordinates)


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


def _finding_from_item(item: Mapping[str, object], customer_id: str, assessment_id: str) -> Finding:
    if item.get("customer_id") != customer_id or item.get("assessment_id") != assessment_id:
        raise AssessmentReportStoreError("finding scope is invalid")
    evidence = item.get("evidence_references")
    if not isinstance(evidence, list) or not all(
        isinstance(reference, str) for reference in evidence
    ):
        raise AssessmentReportStoreError("finding evidence is invalid")
    score = item.get("score")
    if isinstance(score, Decimal):
        score = float(score)
    try:
        return Finding(
            finding_id=item["finding_id"],
            resource_id=item["resource_id"],
            rule_id=item["rule_id"],
            rule_version=item["rule_version"],
            perspective=EvaluationPerspective(item["perspective"]),
            status=EvaluationStatus(item["status"]),
            severity=item["severity"],
            score=score,
            rationale=item["rationale"],
            evidence_references=tuple(evidence),
            # Restore evaluation provenance. Without it every read-back Finding has
            # evaluated_at=None, and annotate_suppressed_findings() skips those
            # (ADR-0020 §6), so read-time suppression would never appear. The two
            # fields are stored together by Finding.to_dict() and must be restored
            # together; a legacy item that stored neither round-trips to None.
            assessed_commit_sha=item.get("assessed_commit_sha"),
            evaluated_at=item.get("evaluated_at"),
        )
    except (KeyError, TypeError, ValueError):
        raise AssessmentReportStoreError("finding is invalid") from None


def _customer_pk(customer_id: str) -> str:
    return f"CUSTOMER#{customer_id}"


def _plan_sk(assessment_id: str) -> str:
    return f"ASSESSMENT#{assessment_id}#PLAN"


def _result_sk_prefix(assessment_id: str) -> str:
    return f"ASSESSMENT#{assessment_id}#RESULT#"


def _finding_sk_prefix(assessment_id: str) -> str:
    return f"ASSESSMENT#{assessment_id}#FINDING#"


def _encode_cursor(
    value: object, customer_id: str, assessment_id: str, prefix: str | None = None
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AssessmentReportStoreError("assessment result page cursor is invalid")
    pk, sk = value.get("PK"), value.get("SK")
    if (
        pk != _customer_pk(customer_id)
        or not isinstance(sk, str)
        or not sk.startswith(prefix or _result_sk_prefix(assessment_id))
    ):
        raise AssessmentReportStoreError("assessment result page cursor is outside scope")
    raw = json.dumps({"PK": pk, "SK": sk}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    cursor: str | None, customer_id: str, assessment_id: str, prefix: str | None = None
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
        or not sk.startswith(prefix or _result_sk_prefix(assessment_id))
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
