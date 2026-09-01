"""DynamoDB persistence for immutable Assessment evaluation results."""

from collections.abc import Mapping
from typing import Protocol

from apps.backend.assessment.findings import DynamoDbFindingStore, finding_from_result
from apps.backend.assessment.worker import EvaluationResultStore
from apps.backend.repositories.dynamodb_values import marshal_item, marshal_value
from packages.contracts import EvaluationResult


class DynamoTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_item(self, **kwargs: object) -> object: ...


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


class EvaluationResultStoreError(RuntimeError):
    """Raised when a result cannot be safely persisted."""


class ImmutableEvaluationResultConflict(EvaluationResultStoreError):
    """Raised when a result key already holds different immutable content."""


class DynamoDbEvaluationResultStore(EvaluationResultStore):
    """Use the documented Resource × Rule × Perspective key as an immutable boundary."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        table_name: str | None = None,
        transaction_client: DynamoTransactionClient | None = None,
    ) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table
        if (table_name is None) != (transaction_client is None):
            raise TypeError("table_name and transaction_client must be supplied together")
        self._table_name = table_name
        self._transaction_client = transaction_client
        self._findings = DynamoDbFindingStore(table)

    def put_if_absent(
        self,
        *,
        customer_id: str,
        assessment_id: str,
        results: tuple[EvaluationResult, ...],
    ) -> None:
        _require_non_empty_string(customer_id, "customer_id")
        _require_non_empty_string(assessment_id, "assessment_id")
        if not results:
            raise ValueError("results must not be empty")
        for result in results:
            if not isinstance(result, EvaluationResult):
                raise TypeError("results must contain EvaluationResult values")
            item = _item_from_result(customer_id, assessment_id, result)
            try:
                if self._transaction_client is not None:
                    self._put_transactional(customer_id, assessment_id, item, result)
                else:
                    self._table.put_item(
                        Item=item,
                        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
                    )
            except Exception as error:
                if _provider_error_code(error) not in {
                    "ConditionalCheckFailedException",
                    "TransactionCanceledException",
                }:
                    raise EvaluationResultStoreError("evaluation result write failed") from None
                if self._existing_item_matches(item):
                    # A transaction can be cancelled by any conditional Put
                    # (including the result itself).  An identical immutable
                    # result is an idempotent replay regardless of provider
                    # error shape.
                    continue
                else:
                    raise ImmutableEvaluationResultConflict(
                        "evaluation result key already contains different content"
                    ) from None
            finding = finding_from_result(result)
            if finding is not None and self._transaction_client is None:
                self._findings.put_if_absent(
                    customer_id=customer_id, assessment_id=assessment_id, finding=finding
                )

    def _put_transactional(
        self,
        customer_id: str,
        assessment_id: str,
        item: dict[str, object],
        result: EvaluationResult,
    ) -> None:
        assert self._transaction_client is not None and self._table_name is not None
        writes: list[dict[str, object]] = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(item),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            }
        ]
        finding = finding_from_result(result)
        if finding is not None:
            finding_item = {
                "PK": f"CUSTOMER#{customer_id}",
                "SK": f"ASSESSMENT#{assessment_id}#FINDING#{finding.finding_id}",
                "entity_type": "FINDING",
                "customer_id": customer_id,
                "assessment_id": assessment_id,
                **finding.to_dict(),
            }
            writes.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": marshal_item(finding_item),
                        "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                    }
                }
            )
        if self._plan_has_counter(customer_id, assessment_id):
            writes.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": marshal_item(
                            {
                                "PK": f"CUSTOMER#{customer_id}",
                                "SK": f"ASSESSMENT#{assessment_id}#PLAN",
                            }
                        ),
                        "UpdateExpression": "ADD completed_evaluations :one",
                        "ConditionExpression": "attribute_exists(PK) AND completed_evaluations < planned_evaluations",
                        "ExpressionAttributeValues": {":one": marshal_value(1)},
                    }
                }
            )
        self._transaction_client.transact_write_items(TransactItems=writes)

    def _plan_has_counter(self, customer_id: str, assessment_id: str) -> bool:
        try:
            item = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"ASSESSMENT#{assessment_id}#PLAN",
                },
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise EvaluationResultStoreError("assessment plan read failed") from None
        return isinstance(item, Mapping) and "completed_evaluations" in item

    def _existing_item_matches(self, expected: dict[str, object]) -> bool:
        try:
            response = self._table.get_item(
                Key={"PK": expected["PK"], "SK": expected["SK"]}, ConsistentRead=True
            )
        except Exception:
            raise EvaluationResultStoreError(
                "evaluation result read after conflict failed"
            ) from None
        existing = response.get("Item")
        return isinstance(existing, Mapping) and dict(existing) == expected


def _item_from_result(
    customer_id: str, assessment_id: str, result: EvaluationResult
) -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": (
            f"ASSESSMENT#{assessment_id}#RESULT#{result.resource_id}#RULE#{result.rule_id}"
            f"#PERSPECTIVE#{result.perspective.value}"
        ),
        "entity_type": "ASSESSMENT_RESULT",
        "customer_id": customer_id,
        "assessment_id": assessment_id,
        **result.to_dict(),
    }


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    details = response.get("Error")
    if not isinstance(details, Mapping):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else None


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
