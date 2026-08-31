"""Unit tests for immutable DynamoDB Assessment result persistence."""

import unittest

from apps.backend.assessment import (
    DynamoDbEvaluationResultStore,
    ImmutableEvaluationResultConflict,
)
from packages.contracts import EvaluationPerspective, EvaluationResult, EvaluationStatus


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        key = (item["PK"], item["SK"])
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = item

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key_data = kwargs["Key"]
        assert isinstance(key_data, dict)
        item = self.items.get((key_data["PK"], key_data["SK"]))
        return {} if item is None else {"Item": item}


def result(
    *,
    score: float = 90,
    status: EvaluationStatus = EvaluationStatus.PASS,
    severity: str = "HIGH",
) -> EvaluationResult:
    return EvaluationResult(
        resource_id="bucket-001",
        rule_id="S3-001",
        perspective=EvaluationPerspective.IAC,
        status=status,
        severity=severity,
        score=score,
        rationale="Public access is blocked",
        evidence_references=("terraform:public-access-block",),
        rule_version="v1",
        rubric_version="mvp-v1",
        model_profile_id="assessment-nova-lite-m0-v1",
    )


class DynamoDbEvaluationResultStoreTest(unittest.TestCase):
    def test_uses_documented_customer_and_result_key(self) -> None:
        table = Table()
        DynamoDbEvaluationResultStore(table).put_if_absent(
            customer_id="cust-001", assessment_id="asm-001", results=(result(),)
        )

        item = next(iter(table.items.values()))
        self.assertEqual(item["PK"], "CUSTOMER#cust-001")
        self.assertEqual(
            item["SK"],
            "ASSESSMENT#asm-001#RESULT#bucket-001#RULE#S3-001#PERSPECTIVE#IAC",
        )

    def test_repeated_delivery_of_identical_result_is_idempotent(self) -> None:
        table = Table()
        store = DynamoDbEvaluationResultStore(table)
        store.put_if_absent(customer_id="cust-001", assessment_id="asm-001", results=(result(),))
        store.put_if_absent(customer_id="cust-001", assessment_id="asm-001", results=(result(),))

        self.assertEqual(len(table.items), 1)

    def test_cannot_replace_an_existing_result(self) -> None:
        table = Table()
        store = DynamoDbEvaluationResultStore(table)
        store.put_if_absent(customer_id="cust-001", assessment_id="asm-001", results=(result(),))

        with self.assertRaises(ImmutableEvaluationResultConflict):
            store.put_if_absent(
                customer_id="cust-001", assessment_id="asm-001", results=(result(score=80),)
            )

    def test_follow_up_result_creates_one_immutable_finding(self) -> None:
        table = Table()
        store = DynamoDbEvaluationResultStore(table)
        failed = result(status=EvaluationStatus.FAIL, score=20)

        store.put_if_absent(customer_id="cust-001", assessment_id="asm-001", results=(failed,))
        store.put_if_absent(customer_id="cust-001", assessment_id="asm-001", results=(failed,))

        findings = [item for item in table.items.values() if item["entity_type"] == "FINDING"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["status"], "FAIL")
        self.assertEqual(findings[0]["rule_id"], "S3-001")
