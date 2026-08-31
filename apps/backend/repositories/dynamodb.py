"""DynamoDB adapter for customer-scoped, revision-checked Job persistence."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from apps.backend.assessment import Assessment
from apps.backend.jobs.lifecycle import InvalidJobTransition, StaleJobRevision, transition_job
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import OutboxStatus, WorkflowOutboxEntry
from apps.backend.repositories.ports import (
    DuplicateJobError,
    InvalidJobMutationError,
    RepositoryError,
    RevisionConflictError,
    StoredDataError,
)
from packages.contracts import ApiError, JobCurrentStep, JobStatus, WorkflowCommand, WorkflowTask


class DynamoTable(Protocol):
    def put_item(self, **kwargs: object) -> object: ...

    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...

    def query(self, **kwargs: object) -> Mapping[str, object]: ...

    def update_item(self, **kwargs: object) -> object: ...


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


class DynamoDbJobRepository:
    """Map Job state to the CUSTOMER# / JOB# key layout in docs/DATABASE.md."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def create_job(self, job: Job) -> None:
        _require_job(job)
        if job.revision != 0 or job.status is not JobStatus.QUEUED:
            raise InvalidJobMutationError("new job must be a QUEUED revision-zero Job")
        try:
            self._table.put_item(
                Item=_item_from_job(job),
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except Exception as error:
            if _provider_error_code(error) == "ConditionalCheckFailedException":
                raise DuplicateJobError("job already exists") from None
            raise RepositoryError("job create failed") from None

    def get_job(self, customer_id: str, job_id: str) -> Job | None:
        _require_non_empty_string(customer_id, "customer_id")
        _require_non_empty_string(job_id, "job_id")
        try:
            response = self._table.get_item(
                Key={"PK": _customer_pk(customer_id), "SK": _job_sk(job_id)},
                ConsistentRead=True,
            )
        except Exception:
            raise RepositoryError("job read failed") from None
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise StoredDataError("stored job item is invalid")
        job = _job_from_item(item)
        if job.customer_id != customer_id:
            raise StoredDataError("stored job customer scope is invalid")
        return job

    def update_job(self, job: Job, *, expected_revision: int) -> None:
        _require_job(job)
        if job.revision != expected_revision + 1:
            raise InvalidJobMutationError("job revision must equal expected_revision + 1")
        current = self.get_job(job.customer_id, job.job_id)
        if current is None or current.revision != expected_revision:
            raise RevisionConflictError("job revision conflict")
        try:
            candidate = transition_job(
                current,
                expected_revision=expected_revision,
                status=job.status,
                current_step=job.current_step,
                assessment_id=job.assessment_id,
                remediation_id=job.remediation_id,
                deployment_id=job.deployment_id,
                error=job.error,
            )
        except (InvalidJobTransition, StaleJobRevision, TypeError, ValueError):
            raise InvalidJobMutationError("job update violates lifecycle") from None
        if candidate != job:
            raise InvalidJobMutationError("job update changes immutable fields")
        try:
            self._table.put_item(
                Item=_item_from_job(job),
                ConditionExpression="#revision = :expected_revision",
                ExpressionAttributeNames={"#revision": "revision"},
                ExpressionAttributeValues={":expected_revision": expected_revision},
            )
        except Exception as error:
            if _provider_error_code(error) == "ConditionalCheckFailedException":
                raise RevisionConflictError("job revision conflict") from None
            raise RepositoryError("job update failed") from None


class DynamoDbAssessmentWorkflowRepository(DynamoDbJobRepository):
    """DynamoDB transaction and outbox adapter for starting an Assessment workflow."""

    def __init__(
        self, table: DynamoTable, *, table_name: str, transaction_client: DynamoTransactionClient
    ) -> None:
        if table is None:
            raise TypeError("table is required")
        _require_non_empty_string(table_name, "table_name")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        super().__init__(table)
        self._table_name = table_name
        self._transaction_client = transaction_client

    def create_assessment_workflow(
        self, assessment: Assessment, job: Job, outbox: WorkflowOutboxEntry
    ) -> None:
        if not isinstance(assessment, Assessment):
            raise TypeError("assessment must be an Assessment")
        _require_job(job)
        if not isinstance(outbox, WorkflowOutboxEntry):
            raise TypeError("outbox must be a WorkflowOutboxEntry")
        if (
            assessment.customer_id != job.customer_id
            or assessment.job_id != job.job_id
            or outbox.customer_id != job.customer_id
            or outbox.job_id != job.job_id
        ):
            raise ValueError("assessment, job, and outbox must have the same customer and job")
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    _transactional_put(self._table_name, _item_from_assessment(assessment)),
                    _transactional_put(self._table_name, _item_from_job(job)),
                    _transactional_put(self._table_name, _item_from_outbox(outbox)),
                ]
            )
        except Exception as error:
            if _provider_error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise DuplicateJobError("assessment workflow already exists") from None
            raise RepositoryError("assessment workflow create failed") from None

    def list_pending_outbox(self, *, limit: int) -> tuple[WorkflowOutboxEntry, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        try:
            response = self._table.query(
                IndexName="GSI2",
                KeyConditionExpression="GSI2PK = :pending",
                ExpressionAttributeValues={":pending": "OUTBOX#PENDING"},
                Limit=limit,
            )
            items = response.get("Items", [])
            if not isinstance(items, list):
                raise TypeError("outbox query items must be a list")
            return tuple(_outbox_from_item(item) for item in items)
        except (TypeError, ValueError):
            raise StoredDataError("stored outbox item is invalid") from None
        except Exception:
            raise RepositoryError("outbox query failed") from None

    def mark_outbox_dispatched(self, entry: WorkflowOutboxEntry) -> None:
        self._update_outbox(entry, status=OutboxStatus.DISPATCHED, increment_attempts=False)

    def record_outbox_dispatch_failure(self, entry: WorkflowOutboxEntry) -> None:
        self._update_outbox(entry, status=OutboxStatus.PENDING, increment_attempts=True)

    def _update_outbox(
        self, entry: WorkflowOutboxEntry, *, status: OutboxStatus, increment_attempts: bool
    ) -> None:
        if not isinstance(entry, WorkflowOutboxEntry):
            raise TypeError("entry must be a WorkflowOutboxEntry")
        expression = "SET #status = :status"
        values: dict[str, object] = {":status": status.value, ":pending": "OUTBOX#PENDING"}
        if status is OutboxStatus.DISPATCHED:
            expression += " REMOVE GSI2PK, GSI2SK"
        if increment_attempts:
            expression += " ADD dispatch_attempts :increment"
            values[":increment"] = 1
        try:
            self._table.update_item(
                Key={"PK": _customer_pk(entry.customer_id), "SK": _outbox_sk(entry.job_id)},
                UpdateExpression=expression,
                ConditionExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
        except Exception:
            raise RepositoryError("outbox update failed") from None


def _item_from_job(job: Job) -> dict[str, object]:
    item: dict[str, object] = {
        "PK": _customer_pk(job.customer_id),
        "SK": _job_sk(job.job_id),
        "entity_type": "JOB",
        "customer_id": job.customer_id,
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status.value,
        "current_step": job.current_step.value,
        "requested_by": job.requested_by,
        "revision": job.revision,
        "GSI1PK": f"JOB#{job.job_id}",
        "GSI1SK": _customer_pk(job.customer_id),
    }
    for name in ("assessment_id", "remediation_id", "deployment_id"):
        if (value := getattr(job, name)) is not None:
            item[name] = value
    if job.error is not None:
        item["error"] = job.error.to_dict()
    return item


def _item_from_assessment(assessment: Assessment) -> dict[str, object]:
    return {
        "PK": _customer_pk(assessment.customer_id),
        "SK": f"ASSESSMENT#{assessment.assessment_id}",
        "entity_type": "ASSESSMENT",
        "customer_id": assessment.customer_id,
        "assessment_id": assessment.assessment_id,
        "job_id": assessment.job_id,
        "repository_id": assessment.repository_id,
        "policy_profile_id": assessment.policy_profile_id,
        "status": "QUEUED",
        "GSI3PK": f"REPOSITORY#{assessment.repository_id}",
        "GSI3SK": f"ASSESSMENT#{assessment.assessment_id}",
    }


def _item_from_outbox(entry: WorkflowOutboxEntry) -> dict[str, object]:
    return {
        "PK": _customer_pk(entry.customer_id),
        "SK": _outbox_sk(entry.job_id),
        "entity_type": "WORKFLOW_OUTBOX",
        "customer_id": entry.customer_id,
        "job_id": entry.job_id,
        "expected_revision": entry.task.expected_revision,
        "command": entry.task.command.value,
        "status": entry.status.value,
        "dispatch_attempts": entry.dispatch_attempts,
        "GSI2PK": "OUTBOX#PENDING",
        "GSI2SK": f"JOB#{entry.job_id}",
    }


def _transactional_put(table_name: str, item: dict[str, object]) -> dict[str, object]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
        }
    }


def _outbox_from_item(item: object) -> WorkflowOutboxEntry:
    value = _mapping(item)
    return WorkflowOutboxEntry(
        customer_id=value["customer_id"],
        job_id=value["job_id"],
        task=WorkflowTask(
            job_id=value["job_id"],
            expected_revision=_stored_revision(value["expected_revision"]),
            command=WorkflowCommand(value["command"]),
        ),
        status=OutboxStatus(value["status"]),
        dispatch_attempts=_stored_revision(value["dispatch_attempts"]),
    )


def _job_from_item(item: Mapping[str, object]) -> Job:
    try:
        error_value = item.get("error")
        error = (
            None
            if error_value is None
            else ApiError(
                code=_mapping(error_value)["code"], message=_mapping(error_value)["message"]
            )
        )
        return Job(
            job_id=item["job_id"],
            customer_id=item["customer_id"],
            job_type=item["job_type"],
            status=JobStatus(item["status"]),
            current_step=JobCurrentStep(item["current_step"]),
            requested_by=item["requested_by"],
            revision=_stored_revision(item["revision"]),
            assessment_id=item.get("assessment_id"),
            remediation_id=item.get("remediation_id"),
            deployment_id=item.get("deployment_id"),
            error=error,
        )
    except (KeyError, TypeError, ValueError):
        raise StoredDataError("stored job item is invalid") from None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError
    return value


def _customer_pk(customer_id: str) -> str:
    return f"CUSTOMER#{customer_id}"


def _job_sk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _outbox_sk(job_id: str) -> str:
    return f"OUTBOX#JOB#{job_id}"


def _stored_revision(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    raise TypeError


def _provider_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    detail = response.get("Error") if isinstance(response, Mapping) else None
    code = detail.get("Code") if isinstance(detail, Mapping) else None
    return code if isinstance(code, str) else None


def _require_job(job: object) -> None:
    if not isinstance(job, Job):
        raise TypeError("job must be a Job")


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
