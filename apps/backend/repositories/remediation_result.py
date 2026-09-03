"""A-owned durable store for the C Remediation Worker result (ADR-0018, ADR-0019 §4).

Worker는 C 소유고 실행 port 구현은 D 소유지만, 결과를 어디에 어떻게 쓰는지는 A의 저장 경계다
(ADR-0018). 결과는 `REMEDIATION#{remediation_id}` item에 conditional update로 한 번만 채운다 —
`DEPLOYMENT#{deployment_id}`에 plan facts를 채우는 것과 같은 관례이고, 이유도 같다: at-least-once
재시도가 같은 값을 다시 쓰려 하면 흡수되고, 다른 결과는 이미 기록된 것을 덮어쓰지 못한다.

별도 `#RESULT` item을 만들지 않는 이유는 Deployment 생성 경로에 있다. 생성은 저장된 decision과
worker 결과를 함께 확인해야 하는데(ADR-0019 §4), 한 item에 있으면 그 확인이 단일 strongly-consistent
get이 된다. 두 item으로 나누면 decision은 보이고 결과는 아직 안 보이는 중간 상태를 읽을 수 있다.
"""

from collections.abc import Mapping
from typing import Protocol

from agent.runtime.github_write_tool import OpenedPullRequest
from apps.backend.remediation.worker import RemediationWork
from apps.backend.repositories.dynamodb_values import marshal_item
from apps.backend.repositories.ports import RepositoryError
from packages.contracts import RemediationAction, RemediationPatch, RemediationSyncTarget


class DynamoTransactionClient(Protocol):
    def transact_write_items(self, **kwargs: object) -> object: ...


class DynamoDbRemediationResultStore:
    """Fill one remediation's worker result exactly once, idempotently."""

    def __init__(self, *, table_name: str, transaction_client: DynamoTransactionClient) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name must be a non-empty string")
        if transaction_client is None:
            raise TypeError("transaction_client is required")
        self._table_name = table_name
        self._transaction_client = transaction_client

    def put_result_if_absent(
        self,
        *,
        work: RemediationWork,
        result: RemediationPatch | RemediationSyncTarget,
    ) -> None:
        if not isinstance(work, RemediationWork):
            raise TypeError("work must be a RemediationWork")
        stored = _result_attribute(work=work, result=result)
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item(
                                {
                                    "PK": f"CUSTOMER#{work.customer_id}",
                                    "SK": f"REMEDIATION#{work.remediation_id}",
                                }
                            ),
                            "UpdateExpression": "SET #result = :result",
                            # `result`는 DynamoDB 예약어라 이름 placeholder가 필요하다.
                            "ExpressionAttributeNames": {"#result": "result"},
                            # Remediation이 존재하고(PK) 아직 결과가 없을 때만 채운다. 조건
                            # 실패는 이미 기록됐다는 뜻이므로 정상 흡수한다(멱등).
                            "ConditionExpression": (
                                "attribute_exists(PK) AND attribute_not_exists(#result)"
                            ),
                            "ExpressionAttributeValues": marshal_item({":result": stored}),
                        }
                    }
                ]
            )
        except Exception as error:
            if _error_code(error) in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                return
            raise RepositoryError("remediation result write failed") from None

    def put_pull_request_if_absent(
        self, *, work: RemediationWork, pull_request: OpenedPullRequest
    ) -> None:
        """Record the pull request GitHub actually opened for this remediation, once.

        PR 번호·URL·head commit은 감사와 화면 표시용 사실이다. Deployment 생성은 이 값이 아니라
        branch 이름으로 merge commit을 다시 찾는다(ADR-0019 §3·§4). 이미 기록돼 있으면 재전달이므로
        흡수한다.
        """
        if not isinstance(work, RemediationWork):
            raise TypeError("work must be a RemediationWork")
        if not isinstance(pull_request, OpenedPullRequest):
            raise TypeError("pull_request must be an OpenedPullRequest")
        if (
            pull_request.customer_id != work.customer_id
            or pull_request.repository_id != work.context.snapshot.repository_id
            or pull_request.finding_id != work.context.finding.finding_id
        ):
            raise ValueError("pull request is outside remediation work")
        try:
            self._transaction_client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item(
                                {
                                    "PK": f"CUSTOMER#{work.customer_id}",
                                    "SK": f"REMEDIATION#{work.remediation_id}",
                                }
                            ),
                            "UpdateExpression": "SET pull_request = :pull_request",
                            # patch result가 먼저 있어야 하고, PR은 한 번만 기록한다.
                            "ConditionExpression": (
                                "attribute_exists(PK) AND attribute_exists(#result) "
                                "AND attribute_not_exists(pull_request)"
                            ),
                            "ExpressionAttributeNames": {"#result": "result"},
                            "ExpressionAttributeValues": marshal_item(
                                {":pull_request": pull_request.to_dict()}
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
                return
            raise RepositoryError("remediation pull request write failed") from None


def _result_attribute(
    *, work: RemediationWork, result: RemediationPatch | RemediationSyncTarget
) -> dict[str, object]:
    """Return the stored result, re-bound to the work it belongs to.

    The Worker already checked the result against its work; re-binding here means a
    store call from another path cannot write a foreign customer's or finding's result
    onto this remediation.
    """
    context = work.context
    snapshot = context.snapshot
    if isinstance(result, RemediationPatch):
        if work.decision.action is not RemediationAction.TERRAFORM_PATCH:
            raise ValueError("a patch result requires a TERRAFORM_PATCH decision")
        if (
            result.finding_id != context.finding.finding_id
            or result.base_commit_sha != snapshot.commit_sha
            or result.artifact.customer_id != work.customer_id
            or result.artifact.repository_id != snapshot.repository_id
        ):
            raise ValueError("patch result is outside remediation work")
        return {"kind": RemediationAction.TERRAFORM_PATCH.value, "patch": result.to_dict()}
    if isinstance(result, RemediationSyncTarget):
        if work.decision.action is not RemediationAction.ACTUAL_SYNC:
            raise ValueError("a sync target result requires an ACTUAL_SYNC decision")
        if (
            result.finding_id != context.finding.finding_id
            or result.customer_id != work.customer_id
            or result.repository_id != snapshot.repository_id
            or result.commit_sha != snapshot.commit_sha
        ):
            raise ValueError("sync target is outside remediation work")
        return {"kind": RemediationAction.ACTUAL_SYNC.value, "sync_target": result.to_dict()}
    raise TypeError("result must be a RemediationPatch or RemediationSyncTarget")


def _error_code(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        details = response.get("Error")
        if isinstance(details, Mapping):
            code = details.get("Code")
            if isinstance(code, str):
                return code
    return type(error).__name__
