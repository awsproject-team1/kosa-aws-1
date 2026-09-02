"""DynamoDB persistence for immutable M2 deployment approvals and audit events."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from apps.backend.deployment import (
    DeploymentApprovalRepository,
    DeploymentRecord,
    DeploymentRecordRepository,
    DeploymentRejection,
)
from apps.backend.deployment.worker import DeploymentWork
from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import WorkflowOutboxEntry
from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.errors import RepositoryError
from apps.backend.repositories.ports import DuplicateJobError, StoredDataError
from packages.contracts import (
    ApplyDispatchReceipt,
    ArtifactReference,
    ArtifactType,
    AuditEventType,
    DeploymentApproval,
    JobStatus,
    PlanExecutionResult,
    TerraformStateVersion,
    WorkflowCommand,
    WorkflowRunFacts,
)
from packages.contracts.remediation import DeploymentReadiness


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


def _error_code(error: BaseException) -> str | None:
    """Extract a DynamoDB error code from a boto3-style exception, if present."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        detail = response.get("Error")
        if isinstance(detail, dict) and isinstance(detail.get("Code"), str):
            return detail["Code"]
    return None


class DynamoReadTable(Protocol):
    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...


class DynamoDbDeploymentApprovalRepository(DeploymentApprovalRepository):
    """Atomically append an exact approval and a metadata-only audit event."""

    def __init__(
        self,
        *,
        table_name: str,
        transaction_client: DynamoTransactionClient,
        table: "DynamoReadTable | None" = None,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client
        # read 경로(`get_approval`)는 자동 un/marshal되는 resource `table`을 쓴다. write만 하는
        # 호출자는 `table`을 주입하지 않아도 되며, 그 경우 read가 fail-closed된다.
        self._table = table
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    def get_approval(self, *, customer_id: str, deployment_id: str) -> DeploymentApproval | None:
        """Read the stored approval for a deployment (ADR-0019 §5·§7).

        승인 item의 key는 결정적(`DEPLOYMENT#{deployment_id}#APPROVAL#approval-{deployment_id}`)
        이므로 한 번의 `get_item`으로 읽는다. read table이 주입되지 않았으면 확인할 수 없으므로
        fail-closed한다.
        """
        for value, name in ((customer_id, "customer_id"), (deployment_id, "deployment_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self._table is None:
            raise RepositoryError("approval read requires a resource table")
        approval_id = f"approval-{deployment_id}"
        try:
            item = self._table.get_item(
                Key={
                    "PK": f"CUSTOMER#{customer_id}",
                    "SK": f"DEPLOYMENT#{deployment_id}#APPROVAL#{approval_id}",
                },
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("deployment approval read failed") from None
        if item is None:
            return None
        if not isinstance(item, Mapping) or item.get("entity_type") != "DEPLOYMENT_APPROVAL":
            raise StoredDataError("stored deployment approval item is invalid")
        approval = _approval_from_item(item)
        if approval.deployment_id != deployment_id:
            raise StoredDataError("stored deployment approval scope is invalid")
        return approval

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


class DynamoDbDeploymentRepository(DeploymentRecordRepository):
    """Create a deployment (record + Job + outbox + audit) and read it back.

    Creation writes four items in one conditional transaction so a deployment
    never exists without its Job and its RUN_DEPLOYMENT outbox (ADR-0019 §4).
    `DeploymentStatus` is not stored; the read path returns only durable facts.
    """

    def __init__(
        self,
        *,
        table: DynamoReadTable,
        table_name: str,
        transaction_client: DynamoTransactionClient,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if table is None or transaction_client is None:
            raise TypeError("table and transaction_client are required")
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        self._table = table
        self._table_name = table_name
        self._transaction_client = transaction_client
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    def create_deployment(
        self, record: DeploymentRecord, *, job: Job, outbox: WorkflowOutboxEntry
    ) -> None:
        if not isinstance(record, DeploymentRecord):
            raise TypeError("record must be a DeploymentRecord")
        if not isinstance(job, Job):
            raise TypeError("job must be a Job")
        if not isinstance(outbox, WorkflowOutboxEntry):
            raise TypeError("outbox must be a WorkflowOutboxEntry")
        if (
            job.customer_id != record.customer_id
            or job.job_id != record.job_id
            or job.deployment_id != record.deployment_id
            or outbox.customer_id != record.customer_id
            or outbox.job_id != record.job_id
            or outbox.task.expected_revision != job.revision
        ):
            raise ValueError("deployment, job, and outbox scope or identifiers are inconsistent")
        if outbox.task.command is not WorkflowCommand.RUN_DEPLOYMENT:
            raise ValueError("deployment outbox command must be RUN_DEPLOYMENT")
        occurred_at = self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        pk = f"CUSTOMER#{record.customer_id}"
        event_id = self._new_id("audit")
        deployment_item = {
            "PK": pk,
            "SK": f"DEPLOYMENT#{record.deployment_id}",
            "entity_type": "DEPLOYMENT",
            "created_at": occurred_at,
            "version": 1,
            **record.to_dict(),
        }
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": record.customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.DEPLOYMENT_REQUESTED.value,
            "deployment_id": record.deployment_id,
            "remediation_id": record.remediation_id,
            "commit_sha": record.commit_sha,
            "plan_hash": record.plan_hash,
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    self._put(deployment_item),
                    self._put(_job_item(job)),
                    self._put(_outbox_item(outbox)),
                    self._put(audit_item),
                ]
            )
        except Exception as error:
            if self._error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise DuplicateJobError("deployment already exists") from None
            raise RepositoryError("deployment create failed") from None

    def reject_deployment(
        self, *, rejection: "DeploymentRejection", cancelled_job: Job, expected_revision: int
    ) -> None:
        """Write a terminal rejection, cancel the Job, and audit it in one transaction."""
        if not isinstance(rejection, DeploymentRejection):
            raise TypeError("rejection must be a DeploymentRejection")
        if not isinstance(cancelled_job, Job):
            raise TypeError("cancelled_job must be a Job")
        if cancelled_job.status is not JobStatus.CANCELLED:
            raise ValueError("cancelled_job must be a CANCELLED Job")
        if cancelled_job.deployment_id != rejection.deployment_id:
            raise ValueError("job and rejection must reference the same deployment")
        customer_id = cancelled_job.customer_id
        occurred_at = rejection.rejected_at
        pk = f"CUSTOMER#{customer_id}"
        event_id = self._new_id("audit")
        # A deterministic rejection key makes reject idempotent and blocks any
        # re-approval of the same deployment (ADR-0019 §8).
        rejection_item = {
            "PK": pk,
            "SK": f"DEPLOYMENT#{rejection.deployment_id}#REJECTION",
            "entity_type": "DEPLOYMENT_REJECTION",
            "customer_id": customer_id,
            "created_at": occurred_at,
            "version": 1,
            **rejection.to_dict(),
        }
        audit_item = {
            "PK": pk,
            "SK": f"AUDIT#{occurred_at}#{event_id}",
            "entity_type": "AUDIT_EVENT",
            "customer_id": customer_id,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "version": 1,
            "event_type": AuditEventType.DEPLOYMENT_REJECTED.value,
            "deployment_id": rejection.deployment_id,
            "reason": rejection.reason.value,
            "rejected_by": rejection.rejected_by,
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(rejection_item),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(_job_item(cancelled_job)),
                            "ConditionExpression": "#revision = :expected",
                            "ExpressionAttributeNames": {"#revision": "revision"},
                            "ExpressionAttributeValues": marshal_item(
                                {":expected": expected_revision}
                            ),
                        }
                    },
                    self._put(audit_item),
                ]
            )
        except Exception as error:
            if self._error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                raise DuplicateJobError("deployment is already rejected or changed") from None
            raise RepositoryError("deployment reject failed") from None

    def get_deployment(self, *, customer_id: str, deployment_id: str) -> DeploymentRecord | None:
        for value, name in ((customer_id, "customer_id"), (deployment_id, "deployment_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"DEPLOYMENT#{deployment_id}"},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("deployment read failed") from None
        if item is None:
            return None
        if not isinstance(item, Mapping) or item.get("entity_type") != "DEPLOYMENT":
            raise StoredDataError("stored deployment item is invalid")
        record = _record_from_item(item)
        if record.customer_id != customer_id or record.deployment_id != deployment_id:
            raise StoredDataError("stored deployment scope is invalid")
        return record

    def _new_id(self, prefix: str) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("generated identifier must be a non-empty string")
        return f"{prefix}-{value}"

    def _put(self, item: dict[str, object]) -> dict[str, object]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": marshal_item(item),
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


class DynamoDbDeploymentPlanStore:
    """Fill the deployment's plan facts once, idempotently (ADR-0019 §1, DATABASE.md M3).

    `RUN_DEPLOYMENT` produces a refreshed plan. Its facts (`plan_hash`, plan/binary
    artifact, state `(lineage, serial)`, and the plan run reference) are written back
    onto the same `DEPLOYMENT#{deployment_id}` item with a conditional update. The
    condition `attribute_not_exists(plan_hash)` makes an at-least-once retry at the
    same revision absorb instead of erroring, while a different plan cannot overwrite
    an already-recorded one. `plan_run` lives here (not on `DeploymentRecord`) because
    the apply step reloads it to download the saved plan artifact (§1).
    """

    def __init__(self, *, table_name: str, transaction_client: DynamoTransactionClient) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client

    def put_plan_if_absent(self, *, work: DeploymentWork, result: PlanExecutionResult) -> None:
        if not isinstance(work, DeploymentWork):
            raise TypeError("work must be a DeploymentWork")
        if not isinstance(result, PlanExecutionResult):
            raise TypeError("result must be a PlanExecutionResult")
        # The worker already checked the result against the work scope; re-bind here
        # so a store call from another path cannot write a foreign plan.
        plan = result.plan
        if plan.deployment_id != work.deployment_id or plan.commit_sha != work.commit_sha:
            raise ValueError("plan is outside the deployment work")
        if plan.artifact.customer_id != work.customer_id:
            raise ValueError("plan is outside the customer scope")
        values = marshal_item(
            {
                ":plan_hash": plan.plan_hash,
                ":plan_artifact": plan.artifact.to_dict(),
                ":binary_artifact": result.binary_artifact.to_dict(),
                ":state_version": result.state_version.to_dict(),
                ":plan_run": result.plan_run.to_dict(),
            }
        )
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item(
                                {
                                    "PK": f"CUSTOMER#{work.customer_id}",
                                    "SK": f"DEPLOYMENT#{work.deployment_id}",
                                }
                            ),
                            "UpdateExpression": (
                                "SET plan_hash = :plan_hash, plan_artifact = :plan_artifact, "
                                "binary_artifact = :binary_artifact, "
                                "state_version = :state_version, plan_run = :plan_run"
                            ),
                            # 배포가 존재하고(PK/SK) 아직 plan이 없을 때만 채운다. 재시도가 같은
                            # 값을 다시 쓰려 하면 조건 실패가 나지만, 그건 이미 저장됐다는 뜻이라
                            # 정상 흡수한다(멱등).
                            "ConditionExpression": (
                                "attribute_exists(PK) AND attribute_not_exists(plan_hash)"
                            ),
                            "ExpressionAttributeValues": values,
                        }
                    }
                ]
            )
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                # 이미 plan이 채워졌거나(멱등 재시도) 배포가 없다. 후자는 create 경로가
                # 보장하므로 여기서는 멱등 흡수로 처리한다.
                return
            raise RepositoryError("deployment plan write failed") from None


class DynamoDbDeploymentRunStore:
    """Record the dispatched apply run once, idempotently (ADR-0019 §5).

    A dispatch acknowledgment carries no `run_id`; it only confirms the apply
    workflow was requested. It is stored under a deterministic
    `DEPLOYMENT#{deployment_id}#DISPATCH` key so a duplicate dispatch at the same
    revision does not create a second record.
    """

    def __init__(self, *, table_name: str, transaction_client: DynamoTransactionClient) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client

    def put_receipt_if_absent(self, *, work: DeploymentWork, receipt: ApplyDispatchReceipt) -> None:
        if not isinstance(work, DeploymentWork):
            raise TypeError("work must be a DeploymentWork")
        if not isinstance(receipt, ApplyDispatchReceipt):
            raise TypeError("receipt must be an ApplyDispatchReceipt")
        if (
            receipt.deployment_id != work.deployment_id
            or receipt.repository_id != work.repository_id
        ):
            raise ValueError("apply dispatch receipt is outside the deployment work")
        item = {
            "PK": f"CUSTOMER#{work.customer_id}",
            "SK": f"DEPLOYMENT#{work.deployment_id}#DISPATCH",
            "entity_type": "DEPLOYMENT_APPLY_DISPATCH",
            "customer_id": work.customer_id,
            "deployment_id": receipt.deployment_id,
            "repository_id": receipt.repository_id,
            "workflow_path": receipt.workflow_path,
            "version": 1,
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(item),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    }
                ]
            )
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                return  # 같은 dispatch가 이미 기록됨(멱등).
            raise RepositoryError("deployment apply dispatch write failed") from None


class DynamoDbDeploymentVerificationStore:
    """Record the authoritative verified run facts once (ADR-0019 §7, DATABASE.md M3).

    The verified `WorkflowRunFacts` (re-read from the run, never trusted from an
    Event) are stored under `DEPLOYMENT#{deployment_id}#EVENT#{run_id}` so the same
    `run_id` is recorded only once. This item is a record, not a state source; the
    deployment status is still derived at read time.
    """

    def __init__(self, *, table_name: str, transaction_client: DynamoTransactionClient) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client

    def put_verification_if_absent(self, *, work: DeploymentWork, facts: WorkflowRunFacts) -> None:
        if not isinstance(work, DeploymentWork):
            raise TypeError("work must be a DeploymentWork")
        if not isinstance(facts, WorkflowRunFacts):
            raise TypeError("facts must be a WorkflowRunFacts")
        if facts.repository_id != work.repository_id:
            raise ValueError("verified run is outside the deployment work")
        item = {
            "PK": f"CUSTOMER#{work.customer_id}",
            "SK": f"DEPLOYMENT#{work.deployment_id}#EVENT#{facts.run_id}",
            "entity_type": "DEPLOYMENT_WORKFLOW_EVENT",
            "customer_id": work.customer_id,
            "deployment_id": work.deployment_id,
            "version": 1,
            **facts.to_dict(),
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(item),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    }
                ]
            )
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                return  # 같은 run_id는 한 번만 기록된다(멱등).
            raise RepositoryError("deployment verification write failed") from None


def _job_item(job: Job) -> dict[str, object]:
    item: dict[str, object] = {
        "PK": f"CUSTOMER#{job.customer_id}",
        "SK": f"JOB#{job.job_id}",
        "entity_type": "JOB",
        "customer_id": job.customer_id,
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status.value,
        "current_step": job.current_step.value,
        "requested_by": job.requested_by,
        "revision": job.revision,
        "GSI1PK": f"JOB#{job.job_id}",
        "GSI1SK": f"CUSTOMER#{job.customer_id}",
    }
    for name in ("assessment_id", "remediation_id", "deployment_id"):
        if (value := getattr(job, name)) is not None:
            item[name] = value
    return item


def _outbox_item(entry: WorkflowOutboxEntry) -> dict[str, object]:
    return {
        "PK": f"CUSTOMER#{entry.customer_id}",
        "SK": f"OUTBOX#JOB#{entry.job_id}",
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


def _approval_from_item(item: Mapping[str, object]) -> DeploymentApproval:
    """Rebuild a DeploymentApproval from its stored item (body = approval.to_dict())."""
    try:
        return DeploymentApproval(
            deployment_id=_text(item, "deployment_id"),
            approved_by=_text(item, "approved_by"),
            commit_sha=_text(item, "commit_sha"),
            plan_hash=_text(item, "plan_hash"),
        )
    except (TypeError, ValueError, KeyError):
        raise StoredDataError("stored deployment approval item is invalid") from None


def _record_from_item(item: Mapping[str, object]) -> DeploymentRecord:
    try:
        plan_hash = _optional_text(item, "plan_hash")
        plan_artifact = (
            None
            if item.get("plan_artifact") is None
            else _artifact_from(item.get("plan_artifact"), ArtifactType.TERRAFORM_PLAN)
        )
        binary_artifact = (
            None
            if item.get("binary_artifact") is None
            else _artifact_from(item.get("binary_artifact"), ArtifactType.TERRAFORM_PLAN_BINARY)
        )
        state_version = (
            None
            if item.get("state_version") is None
            else _state_version_from(item.get("state_version"))
        )
        return DeploymentRecord(
            deployment_id=_text(item, "deployment_id"),
            customer_id=_text(item, "customer_id"),
            repository_id=_text(item, "repository_id"),
            job_id=_text(item, "job_id"),
            remediation_id=_text(item, "remediation_id"),
            commit_sha=_text(item, "commit_sha"),
            source_assessment_id=_text(item, "source_assessment_id"),
            plan_hash=plan_hash,
            plan_artifact=plan_artifact,
            binary_artifact=binary_artifact,
            state_version=state_version,
            verification_assessment_id=_optional_text(item, "verification_assessment_id"),
        )
    except (TypeError, ValueError, KeyError):
        raise StoredDataError("stored deployment item is invalid") from None


def _artifact_from(value: object, expected: ArtifactType) -> ArtifactReference:
    data = _mapping(value)
    artifact = ArtifactReference(
        artifact_id=str(data["artifact_id"]),
        artifact_type=ArtifactType(str(data["artifact_type"])),
        content_sha256=str(data["content_sha256"]),
        customer_id=str(data["customer_id"]),
        repository_id=(None if data.get("repository_id") is None else str(data["repository_id"])),
    )
    if artifact.artifact_type is not expected:
        raise ValueError("stored artifact type does not match the expected type")
    return artifact


def _state_version_from(value: object) -> TerraformStateVersion:
    data = _mapping(value)
    return TerraformStateVersion(lineage=str(data["lineage"]), serial=int(data["serial"]))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StoredDataError("stored nested value is invalid")
    return value


def _text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StoredDataError(f"stored deployment {key} is invalid")
    return value


def _optional_text(item: Mapping[str, object], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StoredDataError(f"stored deployment {key} is invalid")
    return value
