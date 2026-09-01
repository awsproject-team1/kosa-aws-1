"""A-owned DynamoDB storage for approved customer remediation exceptions."""

from collections.abc import Mapping

from apps.backend.repositories.dynamodb import DynamoTable, DynamoTransactionClient
from apps.backend.repositories.ports import DuplicateJobError, RepositoryError, StoredDataError
from packages.contracts import (
    Finding,
    RemediationException,
    RemediationExceptionReason,
)


class DynamoDbRemediationExceptionRepository:
    """Store and read expiring exceptions in one customer partition."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        table_name: str,
        transaction_client: DynamoTransactionClient,
    ) -> None:
        if table is None or transaction_client is None:
            raise TypeError("table and transaction_client are required")
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._table = table
        self._table_name = table_name
        self._transaction_client = transaction_client

    def create_exception(self, exception: RemediationException) -> None:
        if not isinstance(exception, RemediationException):
            raise TypeError("exception must be a RemediationException")
        exception_item = _item_from_exception(exception)
        audit_item = {
            "PK": f"CUSTOMER#{exception.customer_id}",
            "SK": f"AUDIT#{exception.approved_at}#REMEDIATION_EXCEPTION#{exception.exception_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": exception.customer_id,
            "event_type": "REMEDIATION_EXCEPTION_APPROVED",
            "exception_id": exception.exception_id,
            "rule_id": exception.rule_id,
            "rule_version": exception.rule_version,
            "reason": exception.reason.value,
            "approved_by": exception.approved_by,
            "occurred_at": exception.approved_at,
            "version": 1,
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    _put(self._table_name, exception_item),
                    _put(self._table_name, audit_item),
                ]
            )
        except Exception as error:
            if _provider_error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise DuplicateJobError("remediation exception already exists") from None
            raise RepositoryError("remediation exception create failed") from None

    def list_exceptions(
        self, *, customer_id: str, finding: Finding
    ) -> tuple[RemediationException, ...]:
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(finding, Finding):
            raise TypeError("finding must be a Finding")
        prefix = f"REMEDIATION_EXCEPTION#RULE#{finding.rule_id}#VERSION#{finding.rule_version}#"
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
                ExpressionAttributeValues={
                    ":pk": f"CUSTOMER#{customer_id}",
                    ":prefix": prefix,
                },
            )
            items = response.get("Items", [])
            if not isinstance(items, list):
                raise TypeError("exception query items must be a list")
            exceptions = tuple(_exception_from_item(item) for item in items)
        except (KeyError, TypeError, ValueError):
            raise StoredDataError("stored remediation exception is invalid") from None
        except Exception:
            raise RepositoryError("remediation exception query failed") from None
        for exception in exceptions:
            if exception.customer_id != customer_id:
                raise StoredDataError("stored remediation exception customer scope is invalid")
        return tuple(
            exception
            for exception in exceptions
            if exception.resource_id is None or exception.resource_id == finding.resource_id
        )


def _item_from_exception(exception: RemediationException) -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{exception.customer_id}",
        "SK": (
            f"REMEDIATION_EXCEPTION#RULE#{exception.rule_id}#VERSION#{exception.rule_version}#"
            f"EXCEPTION#{exception.exception_id}"
        ),
        "entity_type": "REMEDIATION_EXCEPTION",
        **exception.to_dict(),
        "version": 1,
    }


def _exception_from_item(item: object) -> RemediationException:
    if not isinstance(item, Mapping):
        raise TypeError("stored exception must be a mapping")
    return RemediationException(
        exception_id=item["exception_id"],
        customer_id=item["customer_id"],
        rule_id=item["rule_id"],
        rule_version=item["rule_version"],
        resource_id=item.get("resource_id"),
        reason=RemediationExceptionReason(item["reason"]),
        approved_by=item["approved_by"],
        approved_at=item["approved_at"],
        expires_at=item["expires_at"],
        ticket_reference=item.get("ticket_reference"),
    )


def _put(table_name: str, item: dict[str, object]) -> dict[str, object]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
        }
    }


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None
