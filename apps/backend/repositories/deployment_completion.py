"""A의 apply 완료 Event 예약 write (ADR-0019 §7, DATABASE.md "완료 Event 경계").

완료 Event 경계는 A와 D가 나눠 갖는다. **A는 재조회할 좌표만 예약한다.** GitHub Actions apply run
완료 Event를 받으면 그 event의 `run_id`로 `DEPLOYMENT#{deployment_id}#EVENT#{run_id}` item을
`status=PENDING_VERIFICATION`으로 쓰고, 같은 Deployment의 Job을 다음 revision으로 올리며
`APPLY_COMPLETED` task를 Deployment Queue outbox에 넣는다. **D가 그 좌표로 run을 재조회·대조한
뒤에만** 같은 item을 검증된 `WorkflowRunFacts`로 `VERIFIED` 확정한다.

이 item에 conclusion이나 run facts를 담지 않는 것이 경계의 핵심이다 — Event는 신호이지 정본이
아니다(§7). Event payload를 그대로 저장하면 "성공했다"는 주장이 검증 없이 사실 기록이 되고,
`derive_deployment_status()`가 그걸 apply 성공으로 읽는다. 담는 값은 `run_id`뿐이다.

세 write는 하나의 조건부 transaction이다. Job revision만 올라가고 EVENT 예약이 없으면 D가
`run_reference`를 못 찾아 fail-closed되고, 예약만 되고 task가 없으면 아무도 검증하지 않는다.
Queue payload는 여전히 최소이며 `run_id`를 싣지 않는다 — D는 예약 item에서 좌표를 읽는다.
"""

from apps.backend.jobs.models import Job
from apps.backend.jobs.outbox import WorkflowOutboxEntry
from apps.backend.repositories.deployment import (
    DynamoTransactionClient,
    _error_code,
    _job_item,
    _outbox_item,
)
from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.errors import DuplicateJobError, RepositoryError
from packages.contracts import WorkflowCommand

PENDING_VERIFICATION = "PENDING_VERIFICATION"


class DynamoDbDeploymentCompletionStore:
    """Reserve one apply run's completion coordinate and wake the Deployment Worker."""

    def __init__(self, *, table_name: str, transaction_client: DynamoTransactionClient) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client

    def reserve_completion_event(
        self,
        *,
        deployment_id: str,
        run_id: str,
        resumed_job: Job,
        expected_revision: int,
        outbox: WorkflowOutboxEntry,
        reserved_at: str,
    ) -> None:
        for value, name in (
            (deployment_id, "deployment_id"),
            (run_id, "run_id"),
            (reserved_at, "reserved_at"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(resumed_job, Job):
            raise TypeError("resumed_job must be a Job")
        if not isinstance(outbox, WorkflowOutboxEntry):
            raise TypeError("outbox must be a WorkflowOutboxEntry")
        if resumed_job.deployment_id != deployment_id:
            raise ValueError("resumed job does not belong to this deployment")
        if (
            outbox.customer_id != resumed_job.customer_id
            or outbox.job_id != resumed_job.job_id
            or outbox.task.expected_revision != resumed_job.revision
        ):
            raise ValueError("completion outbox scope or revision is inconsistent")
        if outbox.task.command is not WorkflowCommand.APPLY_COMPLETED:
            raise ValueError("completion outbox command must be APPLY_COMPLETED")

        customer_id = resumed_job.customer_id
        event_item = {
            "PK": f"CUSTOMER#{customer_id}",
            "SK": f"DEPLOYMENT#{deployment_id}#EVENT#{run_id}",
            "entity_type": "DEPLOYMENT_EVENT",
            "customer_id": customer_id,
            "deployment_id": deployment_id,
            # 담는 값은 재조회 좌표뿐이다. conclusion/facts는 D가 run을 다시 읽어 채운다.
            "run_id": run_id,
            "status": PENDING_VERIFICATION,
            "reserved_at": reserved_at,
            "version": 1,
        }
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(event_item),
                            # 같은 run_id는 한 번만 예약된다. 중복 Event 전달이 Job revision을 두 번
                            # 올려 앞선 task를 stale로 만드는 것을 막는다.
                            "ConditionExpression": "attribute_not_exists(SK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(_job_item(resumed_job)),
                            "ConditionExpression": "#revision = :expected",
                            "ExpressionAttributeNames": {"#revision": "revision"},
                            "ExpressionAttributeValues": marshal_item(
                                {":expected": expected_revision}
                            ),
                        }
                    },
                    # Outbox는 Job 하나당 한 칸이고 단계마다 새 task로 다시 채운다(overwrite).
                    # 조건을 걸지 않는 것은 앞 단계의 DISPATCHED 기록을 지우는 것이 정상 동작이기
                    # 때문이다 — Job revision 조건이 이미 중복 재개를 막는다.
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(_outbox_item(outbox)),
                        }
                    },
                ]
            )
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                # 같은 run의 재전달이거나 Job이 이미 다음 revision이다. 어느 쪽이든 예약은 이미
                # 있으므로 중복으로 보고 호출자가 흡수한다.
                raise DuplicateJobError("apply completion is already reserved") from None
            raise RepositoryError("apply completion reservation failed") from None
