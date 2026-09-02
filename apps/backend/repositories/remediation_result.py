"""Immutable DynamoDB persistence for C Remediation Worker results."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from apps.backend.remediation.worker import (
    RemediationResultStore,
    RemediationSyncTarget,
    RemediationWork,
)
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import ArtifactReference, ArtifactType, RemediationAction, RemediationPatch


class DynamoTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def put_item(self, **kwargs: object) -> object: ...


class RemediationResultRepositoryError(RepositoryError):
    """Raised when a remediation result cannot be persisted or loaded safely."""


class ImmutableRemediationResultConflict(RemediationResultRepositoryError):
    """Raised when one remediation identity already has different result content."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredRemediationResult:
    """One immutable Worker result bound to its authoritative Job revision."""

    customer_id: str
    remediation_id: str
    job_id: str
    job_revision: int
    result: RemediationPatch | RemediationSyncTarget

    def __post_init__(self) -> None:
        for name in ("customer_id", "remediation_id", "job_id"):
            _require_non_empty_string(getattr(self, name), name)
        if isinstance(self.job_revision, bool) or not isinstance(self.job_revision, int):
            raise TypeError("job_revision must be an integer")
        if self.job_revision < 0:
            raise ValueError("job_revision must be non-negative")
        if not isinstance(self.result, (RemediationPatch, RemediationSyncTarget)):
            raise TypeError("result must be a RemediationPatch or RemediationSyncTarget")


class DynamoDbRemediationResultRepository(RemediationResultStore):
    """Store one immutable patch or sync target for each Remediation workflow."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table
        self._now = now or (lambda: datetime.now(UTC))

    def put_result_if_absent(
        self,
        *,
        work: RemediationWork,
        result: RemediationPatch | RemediationSyncTarget,
    ) -> None:
        if not isinstance(work, RemediationWork):
            raise TypeError("work must be a RemediationWork")
        _require_result_binding(work, result)
        item = _item_from_result(work, result, occurred_at=self._now_iso())
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except Exception as error:
            if _provider_error_code(error) != "ConditionalCheckFailedException":
                raise RemediationResultRepositoryError("remediation result write failed") from None
            if self._existing_item_matches(item):
                return
            raise ImmutableRemediationResultConflict(
                "remediation result key already contains different immutable content"
            ) from None

    def get_result(
        self, *, customer_id: str, remediation_id: str
    ) -> StoredRemediationResult | None:
        _require_non_empty_string(customer_id, "customer_id")
        _require_non_empty_string(remediation_id, "remediation_id")
        try:
            response = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"REMEDIATION#{remediation_id}#RESULT",
                },
                ConsistentRead=True,
            )
            item = response.get("Item")
            if item is None:
                return None
            stored = _stored_result(_mapping(item))
            if stored.customer_id != customer_id or stored.remediation_id != remediation_id:
                raise StoredDataError("stored remediation result scope is invalid")
            return stored
        except StoredDataError:
            raise
        except (KeyError, TypeError, ValueError):
            raise StoredDataError("stored remediation result is invalid") from None
        except Exception:
            raise RemediationResultRepositoryError("remediation result read failed") from None

    def _existing_item_matches(self, expected: dict[str, object]) -> bool:
        try:
            response = self._table.get_item(
                Key={"PK": expected["PK"], "SK": expected["SK"]},
                ConsistentRead=True,
            )
        except Exception:
            raise RemediationResultRepositoryError(
                "remediation result read after conflict failed"
            ) from None
        existing = response.get("Item")
        if not isinstance(existing, Mapping) or set(existing) != set(expected):
            return False
        if any(
            existing.get(key) != value
            for key, value in expected.items()
            if key not in {"created_at", "updated_at"}
        ):
            return False
        try:
            created_at = _timestamp(existing.get("created_at"), "created_at")
            updated_at = _timestamp(existing.get("updated_at"), "updated_at")
        except (TypeError, ValueError):
            return False
        return created_at == updated_at

    def _now_iso(self) -> str:
        moment = self._now()
        if not isinstance(moment, datetime):
            raise TypeError("now must return a datetime")
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("now must return an offset-aware datetime")
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _item_from_result(
    work: RemediationWork,
    result: RemediationPatch | RemediationSyncTarget,
    *,
    occurred_at: str,
) -> dict[str, object]:
    context = work.context
    return {
        "PK": f"CUSTOMER#{work.customer_id}",
        "SK": f"REMEDIATION#{work.remediation_id}#RESULT",
        "entity_type": "REMEDIATION_RESULT",
        "customer_id": work.customer_id,
        "remediation_id": work.remediation_id,
        "job_id": work.job_id,
        "job_revision": work.revision,
        "result_type": work.decision.action.value,
        "finding_id": context.finding.finding_id,
        "repository_id": context.snapshot.repository_id,
        "commit_sha": context.snapshot.commit_sha,
        "result": _result_dict(result),
        "version": 1,
        "created_at": occurred_at,
        "updated_at": occurred_at,
    }


def _result_dict(result: RemediationPatch | RemediationSyncTarget) -> dict[str, object]:
    if isinstance(result, RemediationPatch):
        return result.to_dict()
    if isinstance(result, RemediationSyncTarget):
        return {
            "finding_id": result.finding_id,
            "customer_id": result.customer_id,
            "repository_id": result.repository_id,
            "commit_sha": result.commit_sha,
        }
    raise TypeError("result must be a RemediationPatch or RemediationSyncTarget")


def _stored_result(item: Mapping[str, object]) -> StoredRemediationResult:
    if item.get("entity_type") != "REMEDIATION_RESULT":
        raise ValueError("stored item is not a remediation result")
    if _integer(item.get("version"), "version") != 1:
        raise ValueError("stored remediation result version is unsupported")
    created_at = _timestamp(item.get("created_at"), "created_at")
    updated_at = _timestamp(item.get("updated_at"), "updated_at")
    if created_at != updated_at:
        raise ValueError("immutable remediation result timestamps must match")
    customer_id = _string(item.get("customer_id"), "customer_id")
    remediation_id = _string(item.get("remediation_id"), "remediation_id")
    job_id = _string(item.get("job_id"), "job_id")
    job_revision = _integer(item.get("job_revision"), "job_revision")
    finding_id = _string(item.get("finding_id"), "finding_id")
    repository_id = _string(item.get("repository_id"), "repository_id")
    commit_sha = _string(item.get("commit_sha"), "commit_sha")
    payload = _mapping(item.get("result"))
    result_type = RemediationAction(item.get("result_type"))

    if result_type is RemediationAction.TERRAFORM_PATCH:
        artifact_value = _mapping(payload.get("artifact"))
        changed_paths = payload.get("changed_paths")
        if not isinstance(changed_paths, list):
            raise TypeError("stored changed_paths must be a list")
        result: RemediationPatch | RemediationSyncTarget = RemediationPatch(
            finding_id=_string(payload.get("finding_id"), "result finding_id"),
            base_commit_sha=_string(payload.get("base_commit_sha"), "base_commit_sha"),
            artifact=ArtifactReference(
                artifact_id=_string(artifact_value.get("artifact_id"), "artifact_id"),
                artifact_type=ArtifactType(artifact_value.get("artifact_type")),
                content_sha256=_string(
                    artifact_value.get("content_sha256"), "artifact content_sha256"
                ),
                customer_id=_string(artifact_value.get("customer_id"), "artifact customer_id"),
                repository_id=artifact_value.get("repository_id"),
            ),
            changed_paths=tuple(_string(path, "changed_paths item") for path in changed_paths),
        )
        if (
            result.finding_id != finding_id
            or result.base_commit_sha != commit_sha
            or result.artifact.customer_id != customer_id
            or result.artifact.repository_id != repository_id
        ):
            raise ValueError("stored patch binding is invalid")
    elif result_type is RemediationAction.ACTUAL_SYNC:
        result = RemediationSyncTarget(
            finding_id=_string(payload.get("finding_id"), "result finding_id"),
            customer_id=_string(payload.get("customer_id"), "result customer_id"),
            repository_id=_string(payload.get("repository_id"), "result repository_id"),
            commit_sha=_string(payload.get("commit_sha"), "result commit_sha"),
        )
        if (
            result.finding_id != finding_id
            or result.customer_id != customer_id
            or result.repository_id != repository_id
            or result.commit_sha != commit_sha
        ):
            raise ValueError("stored sync binding is invalid")
    else:
        raise ValueError("stored remediation result type is not actionable")

    return StoredRemediationResult(
        customer_id=customer_id,
        remediation_id=remediation_id,
        job_id=job_id,
        job_revision=job_revision,
        result=result,
    )


def _require_result_binding(
    work: RemediationWork, result: RemediationPatch | RemediationSyncTarget
) -> None:
    context = work.context
    if work.decision.action is RemediationAction.TERRAFORM_PATCH:
        if not isinstance(result, RemediationPatch):
            raise TypeError("TERRAFORM_PATCH work requires a RemediationPatch result")
        if (
            result.finding_id != context.finding.finding_id
            or result.base_commit_sha != context.snapshot.commit_sha
            or result.artifact.customer_id != work.customer_id
            or result.artifact.repository_id != context.snapshot.repository_id
        ):
            raise ValueError("patch result is outside remediation work")
        return
    if work.decision.action is RemediationAction.ACTUAL_SYNC:
        if not isinstance(result, RemediationSyncTarget):
            raise TypeError("ACTUAL_SYNC work requires a RemediationSyncTarget result")
        if (
            result.finding_id != context.finding.finding_id
            or result.customer_id != work.customer_id
            or result.repository_id != context.snapshot.repository_id
            or result.commit_sha != context.snapshot.commit_sha
        ):
            raise ValueError("sync result is outside remediation work")
        return
    raise ValueError("only actionable remediation work can persist a result")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("stored value must be a mapping")
    return value


def _string(value: object, field_name: str) -> str:
    _require_non_empty_string(value, field_name)
    assert isinstance(value, str)
    return value


def _timestamp(value: object, field_name: str) -> datetime:
    text = _string(value, field_name)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{field_name} must be offset-aware")
    return moment.astimezone(UTC)


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError(f"{field_name} must be an integer")
    integer = int(value)
    if value != integer or integer < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return integer


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    details = response.get("Error")
    if not isinstance(details, Mapping):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else None
