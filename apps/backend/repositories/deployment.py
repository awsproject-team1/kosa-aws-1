"""DynamoDB persistence for immutable M2 deployment approvals and audit events."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from apps.backend.deployment import DeploymentApprovalRepository
from apps.backend.repositories.errors import RepositoryError
from packages.contracts import AuditEventType, DeploymentApproval
from packages.contracts.remediation import DeploymentReadiness


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


class DynamoDbDeploymentApprovalRepository(DeploymentApprovalRepository):
    """Atomically append an exact approval and a metadata-only audit event."""

    def __init__(
        self,
        *,
        table_name: str,
        transaction_client: DynamoTransactionClient,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    def record_approval(
        self, *, customer_id: str, approval: DeploymentApproval, readiness: DeploymentReadiness
    ) -> None:
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(approval, DeploymentApproval) or not isinstance(
            readiness, DeploymentReadiness
        ):
            raise TypeError("approval and readiness must be their respective contracts")
        occurred_at = self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        # One deployment has one approval state.  Keep the approval key
        # deterministic so retries cannot append a second approval record.
        approval_id, event_id = f"approval-{approval.deployment_id}", self._new_id("audit")
        pk = f"CUSTOMER#{customer_id}"
        approval_item = {
            "PK": pk,
            "SK": f"DEPLOYMENT#{approval.deployment_id}#APPROVAL#{approval_id}",
            "entity_type": "DEPLOYMENT_APPROVAL",
            "customer_id": customer_id,
            "approval_id": approval_id,
            "created_at": occurred_at,
            "version": 1,
            **approval.to_dict(),
        }
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.DEPLOYMENT_APPROVED.value,
            "deployment_id": approval.deployment_id,
            "finding_id": readiness.finding_id,
            "commit_sha": approval.commit_sha,
            "plan_hash": approval.plan_hash,
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[self._put(approval_item), self._put(audit_item)]
            )
        except Exception as error:
            if self._error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise RepositoryError("deployment approval already exists") from None
            raise RepositoryError("deployment approval write failed") from None

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("generated identifier must be a non-empty string")
        return f"{prefix}-{value}"

    def _put(self, item: dict[str, object]) -> dict[str, object]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            }
        }

    @staticmethod
    def _error_code(error: BaseException) -> str | None:
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            detail = response.get("Error")
            if isinstance(detail, dict) and isinstance(detail.get("Code"), str):
                return detail["Code"]
        return None
