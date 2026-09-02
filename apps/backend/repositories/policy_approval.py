"""Atomic DynamoDB writer for approved policy sources and published profiles."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.errors import RepositoryError
from packages.contracts import (
    AuditEventType,
    PolicyProfile,
    PolicySourceApproval,
    RuleCandidate,
)


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


class DynamoDbPolicyApprovalRepository:
    """Persist a B-validated decision with its audit event in one transaction.

    Loading reviews remains a separate injected port because C owns candidate
    extraction.  This writer never receives policy text, S3 keys, or a caller
    supplied customer id through its public API service.
    """

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
        self,
        *,
        customer_id: str,
        approval: PolicySourceApproval,
        candidates: tuple[RuleCandidate, ...],
    ) -> None:
        _non_empty(customer_id, "customer_id")
        if not isinstance(approval, PolicySourceApproval) or not all(
            isinstance(candidate, RuleCandidate) for candidate in candidates
        ):
            raise TypeError("approval and candidates are required")
        occurred_at, event_id = self._now_iso(), self._new_id("audit")
        pk = f"CUSTOMER#{customer_id}"
        approval_item = {
            "PK": pk,
            "SK": f"POLICY_SOURCE#{approval.source_id}#VERSION#{approval.source_version}#APPROVAL",
            "entity_type": "POLICY_SOURCE_APPROVAL",
            "customer_id": customer_id,
            "occurred_at": occurred_at,
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
            "event_type": AuditEventType.POLICY_SOURCE_APPROVED.value,
            "source_id": approval.source_id,
            "source_version": approval.source_version,
            "approved_by": approval.approved_by,
        }
        condition = {
            "ConditionCheck": {
                "TableName": self._table_name,
                "Key": marshal_item(
                    {
                        "PK": pk,
                        "SK": f"POLICY_INGESTION#{approval.source_id}#VERSION#{approval.source_version}",
                    }
                ),
                "ConditionExpression": (
                    "customer_id = :customer AND #status = :ready AND artifact_id = :artifact "
                    "AND s3_version_id = :s3_version AND content_sha256 = :digest"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":customer": customer_id,
                        ":ready": "READY",
                        ":artifact": approval.artifact_id,
                        ":s3_version": approval.s3_version_id,
                        ":digest": approval.content_sha256,
                    }
                ),
            }
        }
        self._write([condition, self._put(approval_item), self._put(audit_item)], "policy approval")

    def record_profile(
        self,
        *,
        customer_id: str,
        profile: PolicyProfile,
        published_by: str,
        published_at: str,
    ) -> None:
        _non_empty(customer_id, "customer_id")
        _non_empty(published_by, "published_by")
        _non_empty(published_at, "published_at")
        if not isinstance(profile, PolicyProfile):
            raise TypeError("profile must be a PolicyProfile")
        occurred_at, event_id = self._now_iso(), self._new_id("audit")
        pk = f"CUSTOMER#{customer_id}"
        profile_item = {
            "PK": pk,
            "SK": f"POLICY_PROFILE#{profile.policy_profile_id}#VERSION#{profile.version}",
            "entity_type": "POLICY_PROFILE",
            "customer_id": customer_id,
            "published_by": published_by,
            "published_at": published_at,
            "version": 1,
            **profile.to_dict(),
        }
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.POLICY_PROFILE_PUBLISHED.value,
            "policy_profile_id": profile.policy_profile_id,
            "policy_profile_version": profile.version,
            "published_by": published_by,
        }
        self._write([self._put(profile_item), self._put(audit_item)], "policy profile")

    def _put(self, item: dict[str, object]) -> dict[str, object]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item(item),
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            }
        }

    def _write(self, items: list[dict[str, object]], label: str) -> None:
        try:
            self._transaction_client.transact_write_items(TransactItems=items)
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise RepositoryError(f"{label} already exists or binding is stale") from None
            raise RepositoryError(f"{label} write failed") from None

    def _now_iso(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory()
        _non_empty(value, "generated identifier")
        return f"{prefix}-{value}"


def _non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _error_code(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return code if isinstance(code, str) else None
