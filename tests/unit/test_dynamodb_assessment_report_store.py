"""Assessment report reads a customer-scoped immutable plan and result set."""

import unittest

from apps.backend.assessment import (
    AssessmentEvaluationPlan,
    AssessmentReportNotFoundError,
    DynamoDbAssessmentReportStore,
    DynamoDbEvaluationResultStore,
)
from packages.contracts import EvaluationPerspective, EvaluationResult, EvaluationStatus


class Table:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        self.items[(item["PK"], item["SK"])] = item

    def query(self, **kwargs: object) -> dict[str, object]:
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        prefix = values.get(":assessment", values.get(":results"))
        assert isinstance(prefix, str)
        customer = values[":customer"]
        items = sorted(
            [
                item
                for (pk, sk), item in self.items.items()
                if pk == customer and sk.startswith(prefix)
            ],
            key=lambda item: str(item["SK"]),
        )
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            assert isinstance(start, dict)
            items = [item for item in items if item["SK"] > start["SK"]]
        limit = kwargs.get("Limit")
        if limit is None or len(items) <= limit:
            return {"Items": items}
        assert isinstance(limit, int)
        page = items[:limit]
        return {
            "Items": page,
            "LastEvaluatedKey": {"PK": customer, "SK": page[-1]["SK"]},
        }

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": item}


def result(
    *, resource_id: str = "bucket-001", status: EvaluationStatus = EvaluationStatus.PASS
) -> EvaluationResult:
    return EvaluationResult(
        resource_id=resource_id,
        rule_id="S3-001",
        perspective=EvaluationPerspective.IAC,
        status=status,
        severity="HIGH",
        score=100,
        rationale="Fixture result.",
        evidence_references=("fixture:evidence",),
        rule_version="v1",
        rubric_version="v1",
        model_profile_id="assessment-profile-v1",
    )


class DynamoDbAssessmentReportStoreTest(unittest.TestCase):
    def test_reads_results_and_coverage_from_the_immutable_plan(self) -> None:
        table = Table()
        report_store = DynamoDbAssessmentReportStore(table)
        report_store.put_plan_if_absent(
            AssessmentEvaluationPlan(
                customer_id="cust-001", assessment_id="asm-001", planned_evaluations=2
            )
        )
        DynamoDbEvaluationResultStore(table).put_if_absent(
            customer_id="cust-001", assessment_id="asm-001", results=(result(),)
        )

        report = report_store.get_report(customer_id="cust-001", assessment_id="asm-001")

        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.coverage.completed_evaluations, 1)
        self.assertEqual(report.coverage.percentage, 50)

    def test_cannot_read_an_assessment_without_an_authoritative_plan(self) -> None:
        with self.assertRaises(AssessmentReportNotFoundError):
            DynamoDbAssessmentReportStore(Table()).get_report(
                customer_id="cust-001", assessment_id="asm-001"
            )

    def test_returns_an_opaque_result_cursor_with_full_assessment_coverage(self) -> None:
        table = Table()
        report_store = DynamoDbAssessmentReportStore(table)
        report_store.put_plan_if_absent(
            AssessmentEvaluationPlan(
                customer_id="cust-001", assessment_id="asm-001", planned_evaluations=2
            )
        )
        DynamoDbEvaluationResultStore(table).put_if_absent(
            customer_id="cust-001",
            assessment_id="asm-001",
            results=(result(), result(resource_id="bucket-002")),
        )

        first = report_store.get_report_page(
            customer_id="cust-001", assessment_id="asm-001", limit=1
        )
        second = report_store.get_report_page(
            customer_id="cust-001", assessment_id="asm-001", limit=1, cursor=first.next_cursor
        )

        self.assertEqual(len(first.results), 1)
        self.assertIsNotNone(first.next_cursor)
        self.assertEqual(first.coverage.percentage, 100)
        self.assertEqual(len(second.results), 1)
        self.assertIsNone(second.next_cursor)
